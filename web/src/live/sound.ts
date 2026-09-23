/**
 * 发声：致命级首发在音箱里响一声。
 *
 * **形态**：单次短促一声，非连续、非循环（设计章 4.4）。用 WebAudio 就地合成，
 * 不引任何音频文件（门槛 7：零外链、断网可用）。
 *
 * **谁决定响不响**：`decideAlarmSound()` —— 判据全在服务推来的 `settings.alarm_sound`
 * 与那一条告警的 `muted` 上（见 `alarm.ts`）。这一层只管「把这一声放出来」。
 *
 * **关掉声音这件事有两条路**：持久化开关（`alarm.sound`）与会话级静音，两条都由服务
 * 体现在 `muted` 里，界面不再自己判一次（交付要求第 5 条末句）。
 *
 * **自动播放策略**：页面还没被用户碰过时，浏览器会把 `AudioContext` 按在 `suspended`
 * 上，第一声会放不出来。这里把那次失败**吞掉**，并记下「需要一次用户交互」；
 * `App` 在任意一次点击之后补一次授权（`resume()` + 空跑一个振荡器把链路点着），
 * 之后的声音照放。
 */

/** 只取用到的那几样，测试注入一个假的实现即可（不必是本机的 `AudioContext`）。
 *  `destination` 与 `connect()` 的连线端刻意用 `unknown`：这一层不碰音频图的结构，
 *  只把节点连起来，因此不必依赖 DOM 那套类型。 */
export interface AudioContextLike {
  state: string;
  currentTime: number;
  destination: unknown;
  resume(): Promise<void>;
  createOscillator(): OscillatorLike;
  createGain(): GainLike;
}

export interface OscillatorLike {
  type: string;
  frequency: { value: number };
  connect(target: unknown): unknown;
  start(when?: number): void;
  stop(when?: number): void;
}

export interface GainLike {
  gain: {
    value: number;
    setValueAtTime(value: number, when: number): unknown;
    linearRampToValueAtTime(value: number, when: number): unknown;
  };
  connect(target: unknown): unknown;
}

/** 一声的长短与音高：短促、偏低的「嗒」，与告警的语气相称（不是铃声）。 */
export const BEEP_SECONDS = 0.18;
export const BEEP_FREQUENCY_HZ = 880;
export const BEEP_PEAK_GAIN = 0.25;

/** 造一个 `AudioContext` 的工厂，测试与浏览器各给各的。 */
export type AudioContextFactory = () => AudioContextLike;

/** 浏览器里那一个；没有 WebAudio 时给 `null`（那时界面只是不出声，别的照旧）。 */
export function browserAudioContextFactory(): AudioContextFactory | null {
  const scope = globalThis as unknown as {
    AudioContext?: new () => AudioContextLike;
    webkitAudioContext?: new () => AudioContextLike;
  };
  const Ctor = scope.AudioContext ?? scope.webkitAudioContext;
  if (Ctor === undefined) return null;
  return () => new Ctor();
}

export interface AlarmSoundHandle {
  /**
   * 响一声。返回这一声有没有真的放出来：
   * `false` 表示被自动播放策略按住了（那时调用方记下「要一次用户交互」）。
   */
  play(): boolean;
  /** 用户的第一次交互之后调用：补一次授权，让之后的声音放得出来。 */
  unlock(): void;
  /** 现在解没解锁。 */
  unlocked(): boolean;
}

/**
 * 造一个发声器。
 *
 * `context` 懒建：页面加载时不许先造一个 `AudioContext` 出来（某些浏览器会因此
 * 在控制台记一条警告），第一声要响的时候才建。
 */
export function createAlarmSound(
  createContext: AudioContextFactory | null,
  now: () => number = () => Date.now(),
): AlarmSoundHandle {
  let context: AudioContextLike | null = null;
  let unlocked = false;

  const ensure = (): AudioContextLike | null => {
    if (createContext === null) return null;
    if (context === null) {
      try {
        context = createContext();
      } catch {
        return null;
      }
    }
    return context;
  };

  const beep = (ctx: AudioContextLike): void => {
    const osc = ctx.createOscillator();
    const gain = ctx.createGain();
    const start = ctx.currentTime;
    const end = start + BEEP_SECONDS;
    osc.type = 'square';
    osc.frequency.value = BEEP_FREQUENCY_HZ;
    gain.gain.value = 0;
    gain.gain.setValueAtTime(0, start);
    gain.gain.linearRampToValueAtTime(BEEP_PEAK_GAIN, start + 0.01);
    gain.gain.linearRampToValueAtTime(0, end);
    osc.connect(gain);
    gain.connect(ctx.destination);
    osc.start(start);
    osc.stop(end);
  };

  return {
    play(): boolean {
      const ctx = ensure();
      if (ctx === null) return false;
      if (ctx.state === 'suspended') {
        // 被自动播放策略按住了：吞掉这次失败，等一次用户交互再补。
        void ctx.resume().catch(() => undefined);
        return false;
      }
      try {
        beep(ctx);
      } catch {
        return false;
      }
      return true;
    },

    unlock(): void {
      const ctx = ensure();
      if (ctx === null) return;
      void ctx.resume().catch(() => undefined);
      // 空跑一个振荡器把音频链路点着：某些浏览器要一次真实的发声调用才算授权。
      try {
        const osc = ctx.createOscillator();
        osc.connect(ctx.destination);
        osc.start(ctx.currentTime);
        osc.stop(ctx.currentTime);
      } catch {
        // 点不着也不影响别的：下一次 play() 会再试
      }
      unlocked = true;
      void now;
    },

    unlocked(): boolean {
      return unlocked;
    },
  };
}
