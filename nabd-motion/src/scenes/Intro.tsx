import React from 'react';
import {AbsoluteFill, Easing, interpolate, useCurrentFrame, useVideoConfig} from 'remotion';
import {getLength, getPointAtLength} from '@remotion/paths';
import {Backdrop, Headline, Mark} from '../ui';
import {BOUNCY, C, clamp, FONT, rise, sp} from '../theme';

const ECG =
  'M -40 560 L 600 560 L 660 560 L 690 520 L 720 560 L 800 560 L 840 250 L 900 860 L 955 420 L 990 620 L 1015 560 L 1110 560 L 1140 530 L 1170 560 L 1960 560';
const ECG_LEN = getLength(ECG);

export const IntroScene: React.FC = () => {
  const frame = useCurrentFrame();
  const {fps} = useVideoConfig();

  // 1. The heartbeat draws across the screen.
  const draw = interpolate(frame, [0, 52], [0, 1], {...clamp, easing: Easing.inOut(Easing.cubic)});
  const head = getPointAtLength(ECG, Math.max(0.01, draw * ECG_LEN)) ?? {x: 0, y: 560};
  const lineCollapse = interpolate(frame, [54, 74], [1, 0], {...clamp, easing: Easing.in(Easing.cubic)});
  const lineOpacity = interpolate(frame, [54, 76], [1, 0], clamp);

  // 2. The pulse collapses into the brand mark.
  const markIn = sp(frame, fps, 60, BOUNCY);
  const markDraw = interpolate(frame, [72, 100], [0, 1], {...clamp, easing: Easing.out(Easing.cubic)});
  const slide = sp(frame, fps, 92);
  const ring = interpolate(frame, [60, 100], [0, 1], clamp);

  return (
    <AbsoluteFill>
      <Backdrop />

      {/* Shockwave ring on the beat */}
      <AbsoluteFill style={{justifyContent: 'center', alignItems: 'center'}}>
        <div
          style={{
            width: 260,
            height: 260,
            borderRadius: '50%',
            border: `3px solid ${C.accent}`,
            transform: `scale(${0.4 + ring * 5})`,
            opacity: (1 - ring) * 0.7,
          }}
        />
      </AbsoluteFill>

      {/* ECG line */}
      <AbsoluteFill style={{opacity: lineOpacity, transform: `scaleY(${lineCollapse})`}}>
        <svg width={1920} height={1080} style={{position: 'absolute'}}>
          <defs>
            <linearGradient id="ecgGrad" x1="0" x2="1" y1="0" y2="0">
              <stop offset="0" stopColor={C.accent} stopOpacity="0" />
              <stop offset="0.25" stopColor={C.accent} stopOpacity="0.9" />
              <stop offset="1" stopColor="#bfffe6" />
            </linearGradient>
          </defs>
          <path
            d={ECG}
            fill="none"
            stroke="url(#ecgGrad)"
            strokeWidth={7}
            strokeLinecap="round"
            strokeLinejoin="round"
            strokeDasharray={ECG_LEN}
            strokeDashoffset={ECG_LEN * (1 - draw)}
            style={{filter: `drop-shadow(0 0 14px ${C.accent}) drop-shadow(0 0 40px rgba(46,230,166,0.5))`}}
          />
          <circle cx={head.x} cy={head.y} r={11} fill="#eafff6" style={{filter: `drop-shadow(0 0 22px ${C.accent})`}} />
        </svg>
      </AbsoluteFill>

      {/* Logo lockup */}
      <AbsoluteFill style={{justifyContent: 'center', alignItems: 'center', flexDirection: 'column', gap: 54}}>
        <div style={{display: 'flex', alignItems: 'center', gap: 56, marginTop: -60}}>
          <div
            style={{
              transform: `translateX(${(1 - slide) * 390}px) scale(${markIn}) rotate(${(1 - markIn) * -25}deg)`,
            }}
          >
            <Mark size={230} draw={markDraw} />
          </div>
          <div
            style={{
              display: 'flex',
              alignItems: 'center',
              gap: 40,
              opacity: slide,
              transform: `translateX(${(1 - slide) * 60}px)`,
              clipPath: `inset(0 ${(1 - slide) * 100}% 0 0)`,
            }}
          >
            <span style={{fontFamily: FONT.sans, fontSize: 210, fontWeight: 800, color: C.ivory, letterSpacing: -6}}>
              Nabd
            </span>
            <span style={{width: 3, height: 170, background: C.line}} />
            <span
              style={{
                fontFamily: FONT.sans,
                fontSize: 180,
                fontWeight: 700,
                color: C.accent,
                textShadow: `0 0 50px rgba(46,230,166,0.45)`,
              }}
            >
              نبض
            </span>
          </div>
        </div>

        <div style={{display: 'flex', flexDirection: 'column', alignItems: 'center', gap: 22}}>
          <Headline text="The autonomous SRE agent." delay={112} size={64} weight={600} highlight={{SRE: C.accent}} />
          <div
            style={{
              ...rise(frame, fps, 128, 20),
              fontFamily: FONT.sans,
              fontSize: 32,
              color: C.muted,
              display: 'flex',
              gap: 22,
              alignItems: 'center',
            }}
          >
            {['finds what broke', 'fixes it safely', 'repairs the damage', 'proves it recovered'].map((t, i) => (
              <React.Fragment key={t}>
                {i > 0 && <span style={{color: C.accent}}>•</span>}
                <span style={{opacity: sp(frame, fps, 128 + i * 6)}}>{t}</span>
              </React.Fragment>
            ))}
          </div>
        </div>
      </AbsoluteFill>
    </AbsoluteFill>
  );
};
