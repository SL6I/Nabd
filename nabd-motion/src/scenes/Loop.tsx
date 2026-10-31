import React from 'react';
import {AbsoluteFill, interpolate, useCurrentFrame, useVideoConfig} from 'remotion';
import {Backdrop, Headline, Kicker} from '../ui';
import {BOUNCY, C, clamp, FONT, rise, sp} from '../theme';

const STAGES = [
  {name: 'Detect', desc: 'Prometheus alerts, an OOM forecast that fires before the crash — and Mizan.', color: C.crit},
  {name: 'Triage', desc: 'The dependency graph separates root causes from their symptoms.', color: C.warn},
  {name: 'Investigate', desc: 'Read-only tools: metrics, Drain3 logs, deploys, Redis & Postgres internals.', color: C.accent},
  {name: 'Hypothesize', desc: 'A local signature engine in ~2 s — or an LLM when it gets complex.', color: C.violet},
  {name: 'Skeptic', desc: 'A second pass tries to refute every hypothesis before anything is touched.', color: C.violet},
  {name: 'Act', desc: 'Only safe catalog actions, each with a risk rating and a blast radius.', color: C.accent},
  {name: 'Verify', desc: '3 clean checkouts · SLO met · alerts cleared · the books balance.', color: C.accent},
  {name: 'Report', desc: 'A blameless postmortem in English and Arabic. Remembered for next time.', color: C.gold},
];

const CX = 1250;
const CY = 590;
const R = 330;
const START = 34;
const PER = 32;
const DONE = START + PER * 8;

export const LoopScene: React.FC = () => {
  const frame = useCurrentFrame();
  const {fps} = useVideoConfig();

  let p = 0;
  for (let i = 1; i < STAGES.length; i++) p += sp(frame, fps, START + i * PER, {damping: 18, stiffness: 120});
  const ringIn = sp(frame, fps, 4);
  const active = Math.min(STAGES.length - 1, Math.max(0, Math.floor((frame - START) / PER)));
  const finale = sp(frame, fps, DONE);
  const circ = 2 * Math.PI * R;
  const arcFrac = frame < START ? 0 : interpolate(p, [0, 7], [0.02, 0.875]) + finale * 0.125;
  const angle = ((-90 + p * 45 + finale * 45) * Math.PI) / 180;
  const comet = {x: CX + R * Math.cos(angle), y: CY + R * Math.sin(angle)};

  // Retry arc: Verify (index 6) → Hypothesize (index 3)
  const pos = (i: number, r = R) => {
    const a = ((-90 + i * 45) * Math.PI) / 180;
    return {x: CX + r * Math.cos(a), y: CY + r * Math.sin(a)};
  };
  const v = pos(6, R - 40);
  const h = pos(3, R - 40);
  const retry = interpolate(frame, [START + PER * 6 + 10, START + PER * 6 + 30], [0, 1], clamp);

  const stage = STAGES[active];
  const local = frame - (START + active * PER);

  return (
    <AbsoluteFill>
      <Backdrop glow="rgba(155,123,255,0.12)" />

      <div style={{position: 'absolute', left: 150, top: 300, width: 640}}>
        <Kicker>The recovery loop</Kicker>
        <div style={{height: 26}} />
        <Headline
          text="One loop. The way a senior on-call engineer thinks."
          delay={6}
          size={70}
          highlight={{senior: C.accent}}
        />
        <div
          style={{
            ...rise(frame, fps, 30),
            marginTop: 34,
            fontFamily: FONT.sans,
            fontSize: 28,
            lineHeight: 1.5,
            color: C.muted,
          }}
        >
          It handles several faults at once — and if verification fails, it goes back and tries again.
        </div>
      </div>

      <svg width={1920} height={1080} style={{position: 'absolute'}}>
        <defs>
          <marker id="arrowRed" viewBox="0 0 10 10" refX="6" refY="5" markerWidth="7" markerHeight="7" orient="auto">
            <path d="M0 0 L10 5 L0 10 z" fill={C.crit} />
          </marker>
        </defs>
        <circle
          cx={CX}
          cy={CY}
          r={R}
          fill="none"
          stroke={C.line}
          strokeWidth={3}
          strokeDasharray={circ}
          strokeDashoffset={circ * (1 - ringIn)}
          transform={`rotate(-90 ${CX} ${CY})`}
        />
        <circle
          cx={CX}
          cy={CY}
          r={R}
          fill="none"
          stroke={C.accent}
          strokeWidth={6}
          strokeLinecap="round"
          strokeDasharray={`${circ * arcFrac} ${circ}`}
          transform={`rotate(-90 ${CX} ${CY})`}
          style={{filter: `drop-shadow(0 0 12px ${C.accent})`}}
        />
        {/* Retry path */}
        <path
          d={`M ${v.x} ${v.y} Q ${CX - 40} ${CY + 320} ${h.x} ${h.y}`}
          fill="none"
          stroke={C.crit}
          strokeWidth={3}
          strokeDasharray="10 9"
          strokeDashoffset={-frame}
          opacity={retry * (1 - finale)}
          markerEnd="url(#arrowRed)"
        />
        {frame >= START && (
          <circle cx={comet.x} cy={comet.y} r={14} fill="#eafff6" style={{filter: `drop-shadow(0 0 20px ${C.accent})`}} />
        )}
      </svg>

      {retry > 0 && (
        <div
          style={{
            position: 'absolute',
            left: CX - 150,
            top: CY + 226,
            width: 300,
            textAlign: 'center',
            fontFamily: FONT.mono,
            fontSize: 18,
            color: C.crit,
            opacity: retry * (1 - finale),
          }}
        >
          ↺ verification failed
        </div>
      )}

      {STAGES.map((s, i) => {
        const pt = pos(i);
        const lbl = pos(i, R + 118);
        const appear = sp(frame, fps, 8 + i * 3, BOUNCY);
        const lit = sp(frame, fps, START + i * PER, BOUNCY);
        const isActive = i === active && frame >= START && frame < DONE;
        return (
          <React.Fragment key={s.name}>
            <div
              style={{
                position: 'absolute',
                left: pt.x,
                top: pt.y,
                width: 34,
                height: 34,
                borderRadius: '50%',
                transform: `translate(-50%, -50%) scale(${appear * (1 + (isActive ? 0.35 : 0))})`,
                background: lit > 0.5 ? s.color : C.bg2,
                border: `3px solid ${lit > 0.5 ? s.color : C.faint}`,
                boxShadow: lit > 0.5 ? `0 0 ${isActive ? 40 : 16}px ${s.color}` : 'none',
              }}
            />
            <div
              style={{
                position: 'absolute',
                left: lbl.x,
                top: lbl.y,
                transform: `translate(-50%, -50%) scale(${appear})`,
                fontFamily: FONT.sans,
                fontSize: 30,
                fontWeight: isActive ? 700 : 500,
                color: lit > 0.5 ? C.ivory : C.faint,
                whiteSpace: 'nowrap',
              }}
            >
              {s.name}
            </div>
          </React.Fragment>
        );
      })}

      {/* Center readout */}
      <div
        style={{
          position: 'absolute',
          left: CX - 230,
          top: CY - 150,
          width: 460,
          height: 300,
          display: 'flex',
          flexDirection: 'column',
          alignItems: 'center',
          justifyContent: 'center',
          textAlign: 'center',
        }}
      >
        {frame >= START && frame < DONE && (
          <div key={active} style={{...rise(local + START, fps, START, 24)}}>
            <div style={{fontFamily: FONT.mono, fontSize: 22, color: stage.color, letterSpacing: 4}}>
              {String(active + 1).padStart(2, '0')} / 08
            </div>
            <div style={{fontFamily: FONT.sans, fontSize: 62, fontWeight: 800, color: C.ivory, marginTop: 6}}>
              {stage.name}
            </div>
            <div style={{fontFamily: FONT.sans, fontSize: 24, lineHeight: 1.45, color: C.muted, marginTop: 10}}>
              {stage.desc}
            </div>
          </div>
        )}
        {frame >= DONE && (
          <div style={{...rise(frame, fps, DONE, 24)}}>
            <div style={{fontFamily: FONT.mono, fontSize: 22, color: C.accent, letterSpacing: 4}}>MEDIAN RECOVERY</div>
            <div
              style={{
                fontFamily: FONT.sans,
                fontSize: 130,
                fontWeight: 800,
                color: C.ivory,
                lineHeight: 1,
                textShadow: `0 0 50px rgba(46,230,166,0.4)`,
              }}
            >
              ~55<span style={{fontSize: 60, color: C.accent}}>s</span>
            </div>
            <div style={{fontFamily: FONT.sans, fontSize: 24, color: C.muted, marginTop: 8}}>
              seen it before? the fix costs <b style={{color: C.accent}}>0 tokens</b>
            </div>
          </div>
        )}
      </div>
    </AbsoluteFill>
  );
};
