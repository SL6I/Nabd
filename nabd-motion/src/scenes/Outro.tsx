import React from 'react';
import {AbsoluteFill, Easing, interpolate, useCurrentFrame, useVideoConfig} from 'remotion';
import {Backdrop, Headline, Mark} from '../ui';
import {BOUNCY, C, clamp, FONT, rise, sp} from '../theme';

const BEAT = 480;
const beatPath = (() => {
  let d = 'M 0 150';
  for (let k = 0; k < 6; k++) {
    const o = k * BEAT;
    d += ` L ${o + 180} 150 L ${o + 205} 130 L ${o + 230} 150 L ${o + 260} 150 L ${o + 280} 40 L ${o + 305} 250 L ${o + 330} 110 L ${o + 345} 165 L ${o + 360} 150 L ${o + BEAT} 150`;
  }
  return d;
})();

export const OutroScene: React.FC = () => {
  const frame = useCurrentFrame();
  const {fps} = useVideoConfig();

  const bandIn = interpolate(frame, [0, 22], [0, 1], {...clamp, easing: Easing.out(Easing.exp)});
  const bandOut = interpolate(frame, [82, 104], [0, 1], {...clamp, easing: Easing.in(Easing.cubic)});
  const scroll = (frame * 9) % BEAT;
  const lock = sp(frame, fps, 100, BOUNCY);
  const markDraw = interpolate(frame, [104, 130], [0, 1], clamp);

  return (
    <AbsoluteFill>
      <Backdrop />

      {/* Status band */}
      <div
        style={{
          position: 'absolute',
          left: 0,
          top: 390,
          width: 1920,
          height: 300,
          overflow: 'hidden',
          background: 'linear-gradient(90deg, #0d3a2e, #145a46 50%, #0d3a2e)',
          clipPath: `inset(${bandOut * 50}% ${(1 - bandIn) * 100}% ${bandOut * 50}% 0)`,
          boxShadow: '0 0 120px rgba(46,230,166,0.25)',
        }}
      >
        <svg width={1920 + BEAT} height={300} style={{position: 'absolute', left: -scroll, opacity: 0.35}}>
          <path d={beatPath} fill="none" stroke={C.accent} strokeWidth={4} strokeLinejoin="round" />
        </svg>
        <div
          style={{
            position: 'absolute',
            inset: 0,
            display: 'flex',
            flexDirection: 'column',
            alignItems: 'center',
            justifyContent: 'center',
            gap: 18,
          }}
        >
          <div style={{fontFamily: FONT.sans, fontSize: 124, fontWeight: 800, color: C.ivory, letterSpacing: -4}}>
            All systems nominal
          </div>
          <div style={{display: 'flex', gap: 40, fontFamily: FONT.mono, fontSize: 24, color: '#bff5df'}}>
            {['alerts 0', '3 / 3 checkouts', 'SLO met', 'books balanced'].map((t, i) => (
              <span key={t} style={{opacity: sp(frame, fps, 18 + i * 5)}}>
                ✓ {t}
              </span>
            ))}
          </div>
        </div>
      </div>

      {/* Final lockup */}
      {frame >= 96 && (
        <AbsoluteFill style={{alignItems: 'center', justifyContent: 'center', flexDirection: 'column'}}>
          <div style={{display: 'flex', alignItems: 'center', gap: 46, transform: `scale(${0.8 + lock * 0.2})`, opacity: lock}}>
            <Mark size={170} draw={markDraw} />
            <span style={{fontFamily: FONT.sans, fontSize: 170, fontWeight: 800, color: C.ivory, letterSpacing: -5}}>
              Nabd
            </span>
            <span style={{width: 3, height: 130, background: C.line}} />
            <span
              style={{
                fontFamily: FONT.sans,
                fontSize: 150,
                fontWeight: 700,
                color: C.accent,
                textShadow: '0 0 50px rgba(46,230,166,0.45)',
              }}
            >
              نبض
            </span>
          </div>
          <div style={{marginTop: 46}}>
            <Headline
              text="Finds what broke. Fixes it safely. Repairs the damage. Proves it."
              delay={118}
              size={46}
              weight={500}
              stagger={2}
              highlight={{Proves: C.accent, it: C.ivory}}
              style={{justifyContent: 'center'}}
            />
          </div>
          <div
            style={{
              ...rise(frame, fps, 150, 20),
              marginTop: 60,
              fontFamily: FONT.mono,
              fontSize: 22,
              letterSpacing: 4,
              color: C.faint,
              textTransform: 'uppercase',
            }}
          >
            Hackathon · Challenge 1 — Agentic Recovery of a Failing System
          </div>
        </AbsoluteFill>
      )}
    </AbsoluteFill>
  );
};
