"""`meeting/offline/replay.py` 与 `meeting replay` 入口的契约用例。

全部进程内、全部假件：引擎用本文件的假货（直注入或 `post.engine = module:Class`），
**真引擎一次都不许被构造**（`no_real_engine_can_ever_be_built`）。本文件是**快档**：
不挂 `slow`，单条用例 ≤ 5 秒。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from meeting import cli as cli_module
from meeting.cli import (
    EXIT_FAILURE,
    EXIT_OK,
    EXIT_REJECTED,
    replay as replay_entry,
)
from meeting.offline import replay as replay_module
from meeting.offline.engine import OfflineSegment
from meeting.offline.progress import PROGRESS_NAME, CHUNK_STATES

SAMPLE_RATE = 16000


@pytest.fixture(autouse=True)
def no_real_engine_can_ever_be_built(monkeypatch: pytest.MonkeyPatch) -> Any:
    """**本文件的红线**：真离线引擎一次都不许被构造（绝不加载权重、绝不联网）。"""
    from meeting.offline import engine as engine_module

    class _NeverBuiltEngine:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            raise AssertionError("真引擎不许被构造：本文件的用例必须在假引擎上跑完")

    monkeypatch.setattr(engine_module, "MossOfflineEngine", _NeverBuiltEngine)
    return _NeverBuiltEngine


class FakeReplayEngine:
    """假引擎（直注入档）：记下收到的音频路径，交回两段合成结果。"""

    def __init__(self, *, model: str = "", dtype: str = "") -> None:
        self.model = model
        self.dtype = dtype
        self.calls: list[str] = []

    def transcribe(self, audio_path: Any) -> list[OfflineSegment]:
        self.calls.append(str(audio_path))
        name = Path(str(audio_path)).name
        return [
            OfflineSegment(0.0, 1.0, "S01", f"{name} 的一句"),
            OfflineSegment(1.0, 2.5, "S02", f"{name} 的另一句"),
        ]


class ConfigSelectedEngine:
    """假引擎（配置选择档）：`post.engine = tests.test_offline_replay:ConfigSelectedEngine`。"""

    constructed: list[dict[str, Any]] = []

    def __init__(self, *, model: str = "", dtype: str = "", device: str = "cpu") -> None:
        self.model = model
        self.dtype = dtype
        self.device = device
        ConfigSelectedEngine.constructed.append({"model": model, "dtype": dtype})

    def transcribe(self, audio_path: Any) -> list[OfflineSegment]:
        name = Path(str(audio_path)).name
        return [OfflineSegment(0.0, 1.0, "S01", f"{name} 的一句")]


class BoomEngine(ConfigSelectedEngine):
    """配置选择档的**坏引擎**：每一块都抛错（验「整场 failed → EXIT_FAILURE」用）。"""

    def transcribe(self, audio_path: Any) -> list[OfflineSegment]:
        raise RuntimeError("boom：这一块没跑成")


class FakeConfig:
    """最小配置：只有 `post.*` 几个键（`run_session` / 重放换算只读它们）。"""

    def __init__(self, **values: Any) -> None:
        self.values: dict[str, Any] = {
            "post.engine": "auto",
            "post.model": "OpenMOSS-Team/MOSS-Transcribe-Diarize",
            "post.dtype": "bfloat16",
            "post.chunk_seconds": 450,
            "minutes.hook": "null",
        }
        self.values.update(values)

    def get(self, key: str, default: Any = None) -> Any:
        return self.values.get(key, default)


def synth_session(
    root: Path,
    *,
    sources: tuple[str, ...] = ("mic", "loopback"),
    seconds: float = 10.0,
    empty: tuple[str, ...] = (),
    missing: tuple[str, ...] = (),
) -> Path:
    """合成一个场次目录：各路 16k 单声道裸 PCM + 最小 `session.json`（照 runner 用例同形）。"""
    root.mkdir(parents=True, exist_ok=True)
    for source in sources:
        path = root / f"meeting_audio_{source}_001.pcm"
        if source in missing:
            continue
        if source in empty:
            path.write_bytes(b"")
            continue
        path.write_bytes(b"\x00\x01" * int(seconds * SAMPLE_RATE))
    (root / "session.json").write_text(
        json.dumps(
            {
                "session_id": root.name,
                "dir": str(root),
                "sources": [{"id": source, "archive": f"meeting_audio_{source}_001"} for source in sources],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return root


def digest(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def digest_tree(root: Path) -> dict[str, str]:
    """目录里**每个文件**的 sha256（相对路径 → 摘要）；「原目录一个字节没动」的判据。"""
    taken: dict[str, str] = {}
    for item in sorted(Path(root).rglob("*")):
        if item.is_file():
            taken[item.relative_to(root).as_posix()] = digest(item)
    return taken


def staged_pcm_size(work_dir: Path, source: str) -> int:
    return (work_dir / f"meeting_audio_{source}_001.pcm").stat().st_size


def test_cap_to_seconds_converts_both_forms() -> None:
    assert replay_module.cap_to_seconds() is None
    assert replay_module.cap_to_seconds(seconds=4.5) == 4.5
    assert replay_module.cap_to_seconds(chunks=3, chunk_seconds=2.0) == 6.0


def test_cap_to_seconds_rejects_bad_forms() -> None:
    with pytest.raises(ValueError):
        replay_module.cap_to_seconds(seconds=0)
    with pytest.raises(ValueError):
        replay_module.cap_to_seconds(seconds=-1)
    with pytest.raises(ValueError):
        replay_module.cap_to_seconds(seconds="abc")  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        replay_module.cap_to_seconds(chunks=0)
    with pytest.raises(ValueError):
        replay_module.cap_to_seconds(chunks=2.5)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        # `chunk_seconds = 0`（不分块）时块语义塌缩：如实拒绝，不硬猜
        replay_module.cap_to_seconds(chunks=3, chunk_seconds=0)
    with pytest.raises(ValueError):
        replay_module.cap_to_seconds(seconds=1, chunks=1)


def test_replay_truncates_by_seconds_and_leaves_source_untouched(tmp_path: Path) -> None:
    source = synth_session(tmp_path / "250906", seconds=10.0)
    before = digest_tree(source)

    reading = replay_module.run_replay(
        source,
        seconds=4.0,
        work_dir=tmp_path / "work",
        engine=FakeReplayEngine(),
        config=FakeConfig(),
    )

    # 截断落在了拷贝上：两路各 4.0 秒，整场 8.0 秒
    assert staged_pcm_size(tmp_path / "work", "mic") == 4.0 * SAMPLE_RATE * 2
    assert staged_pcm_size(tmp_path / "work", "loopback") == 4.0 * SAMPLE_RATE * 2
    assert reading["measured_seconds"] == 8.0
    assert reading["per_source"]["mic"]["seconds"] == 4.0
    assert reading["cap_seconds"] == 4.0
    assert reading["rtf"] is not None and reading["rtf"] > 0
    assert reading["counts"]["transcribed"] == 2
    assert reading["summary"]["state"] == "done"
    # 逐块进度记录落在了拷贝目录里（注入的 ledger）
    assert (tmp_path / "work" / PROGRESS_NAME).exists()
    assert digest_tree(source) == before


def test_replay_by_chunks_uses_effective_chunk_seconds(tmp_path: Path) -> None:
    source = synth_session(tmp_path / "250919", seconds=10.0)
    config = FakeConfig(**{"post.chunk_seconds": 2.0})

    reading = replay_module.run_replay(
        source,
        chunks=3,
        work_dir=tmp_path / "work",
        engine=FakeReplayEngine(),
        config=config,
    )

    assert reading["cap_seconds"] == 6.0
    assert reading["measured_seconds"] == 12.0
    assert reading["per_source"]["loopback"]["chunks"] == 3
    rows = reading["chunks"]
    assert len(rows) == 6
    assert all(row["chunks_total"] == 3 for row in rows)
    assert reading["counts"]["transcribed"] == 6


def test_replay_without_cap_runs_whole_session(tmp_path: Path) -> None:
    source = synth_session(tmp_path / "261001", seconds=10.0)

    reading = replay_module.run_replay(
        source,
        work_dir=tmp_path / "work",
        engine=FakeReplayEngine(),
        config=FakeConfig(),
    )

    assert reading["cap_seconds"] is None
    assert reading["measured_seconds"] == 20.0
    assert reading["per_source"]["mic"]["seconds"] == 10.0
    assert reading["counts"]["transcribed"] == 2


def test_replay_rejects_missing_session_dir(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        replay_module.run_replay(tmp_path / "nope", work_dir=tmp_path / "work")


def test_replay_twice_same_material_readings_comparable(tmp_path: Path) -> None:
    """判尺在假引擎上先走一遍流程：两遍结构一致、比值 < 10×。"""
    source = synth_session(tmp_path / "250906", seconds=10.0)
    config = FakeConfig()

    first = replay_module.run_replay(
        source, work_dir=tmp_path / "run1", engine=FakeReplayEngine(), config=config
    )
    second = replay_module.run_replay(
        source, work_dir=tmp_path / "run2", engine=FakeReplayEngine(), config=config
    )

    assert first["summary"]["state"] == second["summary"]["state"] == "done"
    assert first["counts"] == second["counts"]
    # 实时率与总耗时的比值 < 10×（两遍各开一个临时目录，不共址）
    ratio = max(first["rtf"], second["rtf"]) / min(first["rtf"], second["rtf"])
    assert ratio < 10.0


def test_replay_stages_mp3_material_as_pcm(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """MP3 素材走「解码 → 切前 N 秒 → 写段 PCM」那条备料路（链的取音频逻辑零改动）。"""
    source = tmp_path / "260928"
    source.mkdir()
    (source / "meeting_audio_mic_001.mp3").write_bytes(b"\x00" * 16)
    (source / "session.json").write_text(
        json.dumps({"sources": [{"id": "mic", "archive": "meeting_audio_mic_001.mp3"}]}),
        encoding="utf-8",
    )
    wave = np.full(3 * SAMPLE_RATE, 0.5, dtype=np.float32)
    monkeypatch.setattr(replay_module, "decode_audio", lambda _path: wave)

    reading = replay_module.run_replay(
        source,
        seconds=2.0,
        work_dir=tmp_path / "work",
        engine=FakeReplayEngine(),
        config=FakeConfig(),
    )

    assert staged_pcm_size(tmp_path / "work", "mic") == 2.0 * SAMPLE_RATE * 2
    assert reading["measured_seconds"] == 2.0
    assert reading["counts"]["transcribed"] == 1


def test_replay_stages_legacy_unsegmented_names_as_segment_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """旧素材（之前的无段号命名）退认旧名，拷贝落成现行命名（段 001）。"""
    source = tmp_path / "250906"
    source.mkdir()
    (source / "meeting_audio_mic.mp3").write_bytes(b"\x00" * 16)
    (source / "session.json").write_text(
        json.dumps({"sources": [{"id": "mic", "archive": "meeting_audio_mic.mp3"}]}),
        encoding="utf-8",
    )
    wave = np.full(3 * SAMPLE_RATE, 0.5, dtype=np.float32)
    monkeypatch.setattr(replay_module, "decode_audio", lambda _path: wave)

    reading = replay_module.run_replay(
        source,
        seconds=2.0,
        work_dir=tmp_path / "work",
        engine=FakeReplayEngine(),
        config=FakeConfig(),
    )

    # 拷贝里的文件名是现行的段 001 命名，链零改动吃到；读数注明旧素材
    assert staged_pcm_size(tmp_path / "work", "mic") == 2.0 * SAMPLE_RATE * 2
    assert reading["counts"]["transcribed"] == 1
    assert "旧素材命名" in str(reading["per_source"]["mic"]["note"])


def test_replay_stages_legacy_pcm_by_byte_truncation(tmp_path: Path) -> None:
    source = tmp_path / "260907"
    source.mkdir()
    (source / "meeting_audio_loopback.pcm").write_bytes(b"\x00\x01" * (10 * SAMPLE_RATE))
    (source / "session.json").write_text(
        json.dumps({"sources": [{"id": "loopback", "archive": "meeting_audio_loopback.pcm"}]}),
        encoding="utf-8",
    )

    reading = replay_module.run_replay(
        source,
        seconds=4.0,
        work_dir=tmp_path / "work",
        engine=FakeReplayEngine(),
        config=FakeConfig(),
    )

    assert staged_pcm_size(tmp_path / "work", "loopback") == 4.0 * SAMPLE_RATE * 2
    assert reading["per_source"]["loopback"]["seconds"] == 4.0
    assert reading["counts"]["transcribed"] == 1


def test_replay_missing_source_fails_that_source_only(tmp_path: Path) -> None:
    source = synth_session(tmp_path / "260901", seconds=5.0, missing=("mic",))

    reading = replay_module.run_replay(
        source,
        work_dir=tmp_path / "work",
        engine=FakeReplayEngine(),
        config=FakeConfig(),
    )

    assert reading["summary"]["state"] == "partial"
    mic_row = reading["per_source"]["mic"]
    assert mic_row["seconds"] == 0.0
    assert mic_row["note"]  # 照链上 resolve_audio 的说法原样带过来
    assert reading["counts"]["transcribed"] == 1


def test_replay_leaves_no_minutes_files(tmp_path: Path) -> None:
    """只验离线转写链：拷贝目录里零纪要文件（隔离闸 + minutes=False 双保险）。"""
    source = synth_session(tmp_path / "250906", seconds=5.0)

    reading = replay_module.run_replay(
        source,
        work_dir=tmp_path / "work",
        engine=FakeReplayEngine(),
        config=FakeConfig(),
    )

    assert reading["summary"]["minutes"]["hook"] == "skipped"
    assert not (tmp_path / "work" / "meeting_minutes_status.json").exists()
    assert not (tmp_path / "work" / "meeting_final.md").exists()


def test_format_reading_lists_the_three_required_readings(tmp_path: Path) -> None:
    source = synth_session(tmp_path / "250906", seconds=6.0)
    reading = replay_module.run_replay(
        source,
        seconds=6.0,
        work_dir=tmp_path / "work",
        engine=FakeReplayEngine(),
        config=FakeConfig(),
    )

    text = replay_module.format_reading(reading)

    assert "实时率" in text
    assert "每块耗时" in text
    assert "失败原因分布" in text
    assert "transcribed" in text
    assert str(tmp_path / "work") in text


def _run_entry(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, str, str]:
    with pytest.raises(SystemExit) as excinfo:
        replay_entry(list(argv))
    captured = capsys.readouterr()
    return int(excinfo.value.code), captured.out, captured.err


def test_parse_replay_args_shapes(tmp_path: Path) -> None:
    source = synth_session(tmp_path / "250906", seconds=1.0)
    options = cli_module._parse_replay_args(
        [str(source), "--seconds", "30", "--out", str(tmp_path / "out")]
    )
    assert options["dir"] == source.resolve()
    assert options["seconds"] == 30.0
    assert options["chunks"] is None
    assert options["out"] == tmp_path / "out"


def test_replay_entry_usage_errors(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code, out, err = _run_entry(capsys, "--bogus", str(tmp_path))
    assert code == EXIT_REJECTED
    assert "用法：meeting replay" in err
    assert "每路" in err  # --chunks N 按路计：mic、loopback 各领一份 N 块

    code, _out, err = _run_entry(capsys, str(tmp_path), "--seconds")
    assert code == EXIT_REJECTED
    assert "--seconds" in err

    code, _out, _err = _run_entry(capsys)
    assert code == EXIT_REJECTED

    code, _out, err = _run_entry(capsys, str(tmp_path / "nope"))
    assert code == EXIT_REJECTED
    assert "场次目录不存在" in err


def test_replay_entry_runs_with_config_selected_engine(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """入口全链：读配置 → 截断复制 → 按生效 `post.engine` 造引擎 → 打读数 → EXIT_OK。"""
    source = synth_session(tmp_path / "250906", seconds=4.0)
    config = FakeConfig(
        **{
            "post.engine": "tests.test_offline_replay:ConfigSelectedEngine",
            "log.dir": str(tmp_path / "log"),
        },
    )
    monkeypatch.setattr(cli_module, "_load_config_or_exit", lambda **_kwargs: config)
    ConfigSelectedEngine.constructed = []

    code, out, _err = _run_entry(capsys, str(source), "--seconds", "2", "--out", str(tmp_path / "work"))

    assert code == EXIT_OK
    assert "实时率" in out
    assert "失败原因分布" in out
    assert ConfigSelectedEngine.constructed
    assert staged_pcm_size(tmp_path / "work", "mic") == 2 * SAMPLE_RATE * 2
    assert (tmp_path / "work" / "meeting_offline_report.md").exists()


def test_replay_entry_maps_failed_session_to_exit_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    source = synth_session(tmp_path / "250906", seconds=2.0)
    config = FakeConfig(
        **{
            "post.engine": "tests.test_offline_replay:BoomEngine",
            "log.dir": str(tmp_path / "log"),
        }
    )
    monkeypatch.setattr(cli_module, "_load_config_or_exit", lambda **_kwargs: config)

    code, out, _err = _run_entry(capsys, str(source), "--out", str(tmp_path / "work"))

    assert code == EXIT_FAILURE
    assert "failed" in out
    assert "EXIT_FAILURE" in out


def test_replay_entry_asks_for_its_old_log_shape(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """replay() 调共用前奏时点名 replay 旧本的形状：`log_rebuild_failure=False`。

    重建失败的 log 流里不落那行 error 是 replay 旧本没有的形状，这个 kwarg 就是
    还原它的开关——漏传的话 drift 会悄悄回来（替身 `**_kwargs` 会照单全收）。
    """
    seen: dict[str, Any] = {}

    def fake_prelude(**kwargs: Any) -> Any:
        seen.update(kwargs)
        raise SystemExit(EXIT_OK)  # 前奏之后一概不走

    monkeypatch.setattr(cli_module, "_load_config_or_exit", fake_prelude)
    source = tmp_path / "250906"
    source.mkdir()

    with pytest.raises(SystemExit):
        replay_entry([str(source)])

    assert seen == {"log_rebuild_failure": False}


def test_chunk_states_are_the_four_known_states() -> None:
    """读数的四档计数与链上进度记录的四档同表（别自己另立词表）。"""
    assert set(CHUNK_STATES) == {"transcribed", "silent", "failed", "unjudged"}
