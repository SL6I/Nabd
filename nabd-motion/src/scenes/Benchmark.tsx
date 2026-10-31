import React from 'react';
import {AbsoluteFill, interpolate, useCurrentFrame, useVideoConfig} from 'remotion';
import {Backdrop, Glass, Headline, Kicker} from '../ui';
import {C, clamp, FONT, fmt, rise, sp, SNAPPY} from '../theme';

const STATS = [
  {label: 'Root cause identified', nabd: 12, base: 0, max: 12, suffix: ' / 12'},
  {label: 'Recovered & verified', nabd: 12, base: 4, max: 12, suffix: ' / 12'},
  {label: 'Wrong actions taken', nabd: 0, base: 16, max: 16, suffix: '', lowerBetter: true},
  {label: 'Money left charged, no order', nabd: 0, base: 94791, max: 94791, suffix: ' SAR', lowerBetter: true},
];

const FOOT = ['Books balanced 12 / 12', '380 records repaired', 'Median recovery ~55 s', '0 LLM tokens on the core engine'];

export const BenchmarkScene: React.FC = () => {
  const frame = useCurrentFrame();
  const {fps} = useVideoConfig();

  return (
    <AbsoluteFill>
      <Backdrop />
      <div style={{position: 'absolute', left: 150, top: 96, width: 1620}}>
        <Kicker>Benchmark · 12 faults, run for real</Kicker>
        <div style={{height: 18}} />
        <Headline
          text="Nabd vs. “restart whatever is alerting”."
          delay={4}
          size={70}
          highlight={{Nabd: C.accent}}
        />
      </div>

      <div style={{position: 'absolute', left: 150, top: 340, width: 1620, display: 'flex', gap: 30}}>
        {STATS.map((s, i) => {
          const d = 24 + i * 10;
          const enter = sp(frame, fps, d, SNAPPY);
          const count = interpolate(frame, [d + 8, d + 60], [0, 1], {...clamp, easing: (t) => 1 - Math.pow(1 - t, 3)});
          const nabdVal = s.nabd * count;
          const baseVal = s.base * count;
          const bar = (v: number) => (s.max === 0 ? 0 : (v / s.max) * 100);
          return (
            <Glass
              key={s.label}
              style={{
                flex: 1,
                padding: '36px 34px',
                opacity: enter,
                transform: `translateY(${(1 - enter) * 80}px)`,
                display: 'flex',
                flexDirection: 'column',
                gap: 18,
              }}
            >
              <div style={{fontFamily: FONT.sans, fontSize: 26, color: C.muted, height: 70}}>{s.label}</div>
              <div style={{fontFamily: FONT.mono, fontSize: 18, letterSpacing: 3, color: C.accent}}>NABD</div>
              <div
                style={{
                  fontFamily: FONT.sans,
                  fontWeight: 800,
                  fontSize: 104,
                  lineHeight: 1,
                  letterSpacing: -3,
                  color: C.ivory,
                  textShadow: '0 0 40px rgba(46,230,166,0.35)',
                  fontVariantNumeric: 'tabular-nums',
                }}
              >
                {fmt(nabdVal)}
                <span style={{fontSize: 36, color: C.accent, letterSpacing: 0}}>{s.suffix}</span>
              </div>
              <div style={{height: 12, borderRadius: 8, background: 'rgba(242,243,239,0.08)', overflow: 'hidden'}}>
                <div
                  style={{
                    width: `${s.lowerBetter && s.nabd === 0 ? 1.5 : bar(nabdVal)}%`,
                    height: '100%',
                    background: C.accent,
                    boxShadow: `0 0 14px ${C.accent}`,
                  }}
                />
              </div>
              <div style={{marginTop: 18, fontFamily: FONT.mono, fontSize: 18, letterSpacing: 3, color: C.crit}}>
                RUNBOOK
              </div>
              <div
                style={{
                  fontFamily: FONT.sans,
                  fontWeight: 700,
                  fontSize: 46,
                  color: C.crit,
                  fontVariantNumeric: 'tabular-nums',
                }}
              >
                {fmt(baseVal)}
                <span style={{fontSize: 22}}>{s.suffix}</span>
              </div>
              <div style={{height: 12, borderRadius: 8, background: 'rgba(242,243,239,0.08)', overflow: 'hidden'}}>
                <div
                  style={{
                    width: `${Math.max(1.5, bar(baseVal))}%`,
                    height: '100%',
                    background: C.crit,
                    boxShadow: `0 0 14px ${C.crit}`,
                  }}
                />
              </div>
            </Glass>
          );
        })}
      </div>

      <div
        style={{
          position: 'absolute',
          left: 150,
          top: 960,
          width: 1620,
          display: 'flex',
          justifyContent: 'space-between',
        }}
      >
        {FOOT.map((f, i) => (
          <div
            key={f}
            style={{
              ...rise(frame, fps, 110 + i * 8, 20),
              fontFamily: FONT.sans,
              fontSize: 28,
              color: C.ivory,
              display: 'flex',
              alignItems: 'center',
              gap: 14,
            }}
          >
            <span style={{color: C.accent, fontSize: 22}}>◆</span>
            {f}
          </div>
        ))}
      </div>
    </AbsoluteFill>
  );
};
