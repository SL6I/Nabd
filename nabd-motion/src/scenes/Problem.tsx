import React from 'react';
import {AbsoluteFill, interpolate, random, useCurrentFrame, useVideoConfig} from 'remotion';
import {Backdrop, Dot, Glass, Headline, Kicker} from '../ui';
import {C, clamp, FONT, fmt, rise, sp, SNAPPY} from '../theme';

const METRICS = [
  {label: 'Error rate', value: '0.00%'},
  {label: 'p95 latency', value: '84 ms', spark: true},
  {label: 'Alerts firing', value: '0'},
  {label: 'Services healthy', value: '7 / 7'},
];

const GLITCH = 104;

export const ProblemScene: React.FC = () => {
  const frame = useCurrentFrame();
  const {fps} = useVideoConfig();

  const glitching = frame >= GLITCH && frame < GLITCH + 14;
  const jitter = (k: number) => (glitching ? (random(`j${frame}${k}`) - 0.5) * 34 : 0);
  const flash = interpolate(frame, [GLITCH, GLITCH + 4, GLITCH + 26], [0, 0.32, 0], clamp);
  const turned = sp(frame, fps, GLITCH + 4);
  const money = interpolate(frame, [GLITCH + 30, GLITCH + 100], [0, 94791], {
    ...clamp,
    easing: (t) => 1 - Math.pow(1 - t, 3),
  });

  return (
    <AbsoluteFill>
      <Backdrop
        glow={turned > 0.5 ? 'rgba(255,61,90,0.16)' : undefined}
        glow2={`rgba(${Math.round(15 + turned * 90)}, ${Math.round(107 - turned * 80)}, ${Math.round(82 - turned * 40)}, 0.35)`}
      />

      <AbsoluteFill style={{padding: '120px 150px', flexDirection: 'column'}}>
        <Kicker>The quiet failure</Kicker>
        <div style={{height: 26}} />
        <div
          style={{
            transform: `translateX(${jitter(1)}px)`,
            textShadow: glitching ? `${jitter(2) / 3}px 0 ${C.crit}, ${jitter(3) / 3}px 0 #3cf` : undefined,
          }}
        >
          <Headline text="Every request returns 200." delay={6} size={96} highlight={{'200': C.accent}} />
        </div>

        {/* Healthy dashboard */}
        <div
          style={{
            display: 'flex',
            gap: 34,
            marginTop: 70,
            transform: `translate(${jitter(4)}px, ${jitter(5) / 2}px) scale(${1 - turned * 0.04})`,
            filter: `saturate(${1 - turned * 0.85}) brightness(${1 - turned * 0.35})`,
          }}
        >
          {METRICS.map((m, i) => {
            const s = sp(frame, fps, 26 + i * 8, SNAPPY);
            return (
              <Glass
                key={m.label}
                style={{
                  flex: 1,
                  padding: '34px 36px',
                  opacity: s,
                  transform: `translateY(${(1 - s) * 70}px)`,
                }}
              >
                <div style={{display: 'flex', alignItems: 'center', justifyContent: 'space-between'}}>
                  <span style={{fontFamily: FONT.sans, fontSize: 26, color: C.muted}}>{m.label}</span>
                  <span
                    style={{
                      display: 'flex',
                      alignItems: 'center',
                      gap: 10,
                      fontFamily: FONT.mono,
                      fontSize: 20,
                      color: C.accent,
                    }}
                  >
                    <Dot color={C.accent} size={12} pulse={(Math.sin(frame / 6 + i) + 1) / 2} /> OK
                  </span>
                </div>
                <div
                  style={{
                    fontFamily: FONT.sans,
                    fontSize: 78,
                    fontWeight: 700,
                    color: C.ivory,
                    marginTop: 16,
                    letterSpacing: -2,
                  }}
                >
                  {m.value}
                </div>
                <svg width="100%" height={46} viewBox="0 0 300 46" preserveAspectRatio="none">
                  <path
                    d={Array.from({length: 31}, (_, k) => {
                      const y = 30 + (random(`s${i}${k}`) - 0.5) * (m.spark ? 8 : 3);
                      return `${k === 0 ? 'M' : 'L'} ${k * 10} ${y}`;
                    }).join(' ')}
                    fill="none"
                    stroke={C.accent}
                    strokeWidth={3}
                    strokeOpacity={0.8}
                    pathLength={1}
                    strokeDasharray={1}
                    strokeDashoffset={1 - interpolate(frame, [34 + i * 8, 80 + i * 8], [0, 1], clamp)}
                  />
                </svg>
              </Glass>
            );
          })}
        </div>

        {/* The twist */}
        <div style={{display: 'flex', alignItems: 'flex-end', justifyContent: 'space-between', marginTop: 70}}>
          <Headline
            text="…and the money still disappears."
            delay={GLITCH + 8}
            size={80}
            stagger={4}
            highlight={{money: C.crit, disappears: C.crit}}
            style={{maxWidth: 900}}
          />
          <div style={{textAlign: 'right', ...rise(frame, fps, GLITCH + 26, 40)}}>
            <div
              style={{
                fontFamily: FONT.sans,
                fontSize: 150,
                fontWeight: 800,
                color: C.crit,
                letterSpacing: -4,
                lineHeight: 1,
                textShadow: `0 0 60px rgba(255,61,90,0.5)`,
                fontVariantNumeric: 'tabular-nums',
              }}
            >
              {fmt(money)}
              <span style={{fontSize: 56, marginLeft: 16, fontWeight: 600}}>SAR</span>
            </div>
            <div style={{fontFamily: FONT.sans, fontSize: 26, color: C.muted, marginTop: 14, maxWidth: 760}}>
              charged to customers with <b style={{color: C.ivory}}>no order</b>, after a classic
              “restart whatever is alerting” runbook
            </div>
            <div style={{fontFamily: FONT.mono, fontSize: 20, letterSpacing: 2, color: C.faint, marginTop: 12}}>
              TEST STORE · 12 INJECTED FAULTS
            </div>
          </div>
        </div>
      </AbsoluteFill>

      <AbsoluteFill style={{background: C.crit, opacity: flash, mixBlendMode: 'screen'}} />
    </AbsoluteFill>
  );
};
