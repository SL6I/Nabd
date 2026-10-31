import {loadFont as loadAlexandria} from '@remotion/google-fonts/Alexandria';
import {loadFont as loadMono} from '@remotion/google-fonts/JetBrainsMono';
import {interpolate, spring, SpringConfig} from 'remotion';

// Nabd's identity, translated to a dark cinematic palette:
// deep emerald carries the pulse, crimson only when something fails,
// gold for Mizan's books, violet for the LLM.
const alexandria = loadAlexandria('normal', {
  weights: ['300', '400', '500', '600', '700', '800'],
  subsets: ['arabic', 'latin'],
});
const jetbrains = loadMono('normal', {
  weights: ['400', '500', '700'],
  subsets: ['latin'],
});

export const FONT = {
  sans: alexandria.fontFamily,
  mono: jetbrains.fontFamily,
};

export const C = {
  ink: '#030d0a',
  bg: '#04140f',
  bg2: '#082219',
  emerald: '#123f33',
  emerald2: '#0f6b52',
  accent: '#2ee6a6',
  accentSoft: 'rgba(46, 230, 166, 0.14)',
  ivory: '#f2f3ef',
  muted: 'rgba(242, 243, 239, 0.62)',
  faint: 'rgba(242, 243, 239, 0.32)',
  line: 'rgba(242, 243, 239, 0.12)',
  crit: '#ff3d5a',
  critSoft: 'rgba(255, 61, 90, 0.14)',
  warn: '#ffb020',
  gold: '#f0bd55',
  goldSoft: 'rgba(240, 189, 85, 0.14)',
  violet: '#9b7bff',
  violetSoft: 'rgba(155, 123, 255, 0.16)',
};

export const W = 1920;
export const H = 1080;
export const FPS = 30;

// The brand mark's pulse, in a 40x40 box.
export const MARK_PATH = 'M6 22h7l3-9 5 15 4-11 2 5h7';

export const SMOOTH: Partial<SpringConfig> = {damping: 200};
export const SNAPPY: Partial<SpringConfig> = {damping: 16, stiffness: 170, mass: 0.7};
export const BOUNCY: Partial<SpringConfig> = {damping: 11, stiffness: 160, mass: 0.6};

export const sp = (
  frame: number,
  fps: number,
  delay = 0,
  config: Partial<SpringConfig> = SMOOTH,
  durationInFrames?: number,
) => spring({frame: frame - delay, fps, config, durationInFrames});

/** Fade + rise entrance driven by a spring. */
export const rise = (frame: number, fps: number, delay = 0, distance = 40, config = SMOOTH) => {
  const s = sp(frame, fps, delay, config);
  return {
    opacity: interpolate(s, [0, 1], [0, 1]),
    transform: `translateY(${interpolate(s, [0, 1], [distance, 0])}px)`,
  } as const;
};

/** Fade out between two frames. */
export const fadeOut = (frame: number, start: number, end: number) =>
  interpolate(frame, [start, end], [1, 0], {extrapolateLeft: 'clamp', extrapolateRight: 'clamp'});

export const clamp = {extrapolateLeft: 'clamp', extrapolateRight: 'clamp'} as const;

export const fmt = (n: number) => Math.round(n).toLocaleString('en-US');
