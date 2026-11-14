"""`tests/test_drain` 的包级夹具：收场把本包进程内用例起过的转写流水线收干净。

判据是「**任意收集序下泄漏都不出现**」：`_inprocess_runtime()` /
`_inprocess_runtime_with_backends()` 造的运行时一经 `start_session()` 就起真转写流水线
线程（asr-worker / asr-reader-*，daemon），不经 `shutdown()` 不退 —— 收集序把本包排到
`tests/test_asr_pipeline.py` 之前时，那边 autouse 的线程闸每条用例都红（改名序
`tests/drain` 实测 29 passed 44 errors）。登记与收场的手法与 `tests/test_transcode_job.py`
的 `_join_transcriber_pipelines` 同一份。
"""

import pytest

from tests.test_drain import _common


@pytest.fixture(autouse=True)
def _join_transcriber_pipelines():
    """每条用例收场把本包登记过的运行时的转写流水线 join 干净（线程不漏出本包）。"""
    yield
    _common.stop_registered_transcriber_pipelines()
