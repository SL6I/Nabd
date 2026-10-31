import React from 'react';
import {AbsoluteFill, interpolate, useCurrentFrame, useVideoConfig} from 'remotion';
import {C, FONT, MARK_PATH, rise, sp} from './theme';

/** Living backdrop: deep emerald, a drifting glow, a slow grid and film grain. */
export const Backdrop: React.FC<{glow?: string; glow2?: string; gridOpacity?: number}> = ({
  glow = 'rgba(46, 230, 166, 0.16)',
  glow2 = 'rgba(15, 107, 82, 0.35)',
  gridOpacity = 1,
}) => {
  const frame = useCurrentFrame();
  const dx = Math.sin(frame / 90) * 120;
  const dy = Math.cos(frame / 110) * 80;
  const gridShift = (frame * 0.6) % 80;
  return (
    <AbsoluteFill style={{background: C.bg, overflow: 'hidden'}}>
      <AbsoluteFill
        style={{
          background: `radial-gradient(1100px 760px at ${960 + dx}px ${420 + dy}px, ${glow2}, transparent 70%)`,
        }}
      />
      <AbsoluteFill
        style={{
          background: `radial-gradient(700px 520px at ${1500 - dx}px ${820 - dy}px, ${glow}, transparent 70%)`,
        }}
      />
      <AbsoluteFill
        style={{
          opacity: 0.55 * gridOpacity,
          backgroundImage: `linear-gradient(${C.line} 1px, transparent 1px), linear-gradient(90deg, ${C.line} 1px, transparent 1px)`,
          backgroundSize: '80px 80px',
          backgroundPosition: `${gridShift}px ${gridShift}px`,
          maskImage: 'radial-gradient(ellipse 75% 70% at 50% 50%, black 20%, transparent 80%)',
          WebkitMaskImage: 'radial-gradient(ellipse 75% 70% at 50% 50%, black 20%, transparent 80%)',
        }}
      />
      <Grain />
      <AbsoluteFill
        style={{background: 'radial-gradient(ellipse 85% 80% at 50% 50%, transparent 55%, rgba(0,0,0,0.65) 100%)'}}
      />
    </AbsoluteFill>
  );
};

export const Grain: React.FC = () => {
  const frame = useCurrentFrame();
  const seed = frame % 6;
  return (
    <AbsoluteFill style={{opacity: 0.07, mixBlendMode: 'overlay', pointerEvents: 'none'}}>
      <svg width="100%" height="100%">
        <filter id={`grain-${seed}`}>
          <feTurbulence type="fractalNoise" baseFrequency="0.9" numOctaves="2" seed={seed} stitchTiles="stitch" />
          <feColorMatrix type="saturate" values="0" />
        </filter>
        <rect width="100%" height="100%" filter={`url(#grain-${seed})`} />
      </svg>
    </AbsoluteFill>
  );
};

/** The Nabd brand mark: emerald tile with the pulse drawn in ivory. */
export const Mark: React.FC<{size: number; draw?: number; glow?: boolean}> = ({size, draw = 1, glow = true}) => (
  <svg width={size} height={size} viewBox="0 0 40 40" style={{overflow: 'visible'}}>
    <defs>
      <linearGradient id="markFill" x1="0" y1="0" x2="1" y2="1">
        <stop offset="0" stopColor="#1a5a48" />
        <stop offset="1" stopColor="#0b2c23" />
      </linearGradient>
    </defs>
    <rect
      width="40"
      height="40"
      rx="11"
      fill="url(#markFill)"
      stroke="rgba(46,230,166,0.45)"
      strokeWidth="0.6"
      style={glow ? {filter: 'drop-shadow(0 0 6px rgba(46,230,166,0.45))'} : undefined}
    />
    <path
      d={MARK_PATH}
      fill="none"
      stroke={C.ivory}
      strokeWidth="3"
      strokeLinecap="round"
      strokeLinejoin="round"
      pathLength={1}
      strokeDasharray={1}
      strokeDashoffset={1 - draw}
    />
  </svg>
);

/** Small uppercase label above a title. */
export const Kicker: React.FC<{children: React.ReactNode; color?: string; delay?: number}> = ({
  children,
  color = C.accent,
  delay = 0,
}) => {
  const frame = useCurrentFrame();
  const {fps} = useVideoConfig();
  const s = sp(frame, fps, delay);
  return (
    <div
      style={{
        ...rise(frame, fps, delay, 20),
        display: 'inline-flex',
        alignItems: 'center',
        gap: 14,
        fontFamily: FONT.mono,
        fontSize: 24,
        letterSpacing: 6,
        textTransform: 'uppercase',
        color,
      }}
    >
      <span style={{width: interpolate(s, [0, 1], [0, 48]), height: 2, background: color, display: 'block'}} />
      {children}
    </div>
  );
};

/** Word-by-word headline reveal. */
export const Headline: React.FC<{
  text: string;
  delay?: number;
  size?: number;
  color?: string;
  weight?: number;
  stagger?: number;
  highlight?: Record<string, string>;
  style?: React.CSSProperties;
}> = ({text, delay = 0, size = 84, color = C.ivory, weight = 700, stagger = 3, highlight = {}, style}) => {
  const frame = useCurrentFrame();
  const {fps} = useVideoConfig();
  const words = text.split(' ');
  return (
    <div
      style={{
        fontFamily: FONT.sans,
        fontSize: size,
        fontWeight: weight,
        lineHeight: 1.12,
        letterSpacing: -1.5,
        color,
        display: 'flex',
        flexWrap: 'wrap',
        columnGap: size * 0.26,
        ...style,
      }}
    >
      {words.map((w, i) => {
        const s = sp(frame, fps, delay + i * stagger);
        const clean = w.replace(/[.,:!?]/g, '');
        return (
          <span
            key={i}
            style={{
              display: 'inline-block',
              opacity: s,
              transform: `translateY(${(1 - s) * size * 0.5}px)`,
              filter: `blur(${(1 - s) * 8}px)`,
              color: highlight[clean] ?? color,
            }}
          >
            {w}
          </span>
        );
      })}
    </div>
  );
};

/** Frosted glass panel. */
export const Glass: React.FC<{children: React.ReactNode; style?: React.CSSProperties; tint?: string}> = ({
  children,
  style,
  tint = 'rgba(242,243,239,0.045)',
}) => (
  <div
    style={{
      background: `linear-gradient(160deg, ${tint}, rgba(242,243,239,0.015))`,
      border: `1px solid ${C.line}`,
      borderRadius: 28,
      boxShadow: '0 30px 80px rgba(0,0,0,0.45), inset 0 1px 0 rgba(255,255,255,0.06)',
      backdropFilter: 'blur(14px)',
      ...style,
    }}
  >
    {children}
  </div>
);

export const Dot: React.FC<{color: string; size?: number; pulse?: number}> = ({color, size = 14, pulse = 0}) => (
  <span
    style={{
      width: size,
      height: size,
      borderRadius: '50%',
      background: color,
      display: 'inline-block',
      boxShadow: `0 0 ${8 + pulse * 18}px ${color}`,
      flexShrink: 0,
    }}
  />
);
