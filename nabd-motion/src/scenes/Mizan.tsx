import React from 'react';
import {AbsoluteFill, interpolate, useCurrentFrame, useVideoConfig} from 'remotion';
import {Backdrop, Glass, Headline, Kicker} from '../ui';
import {BOUNCY, C, clamp, FONT, fmt, rise, sp, SNAPPY} from '../theme';

const ROWS = [
  {pay: 'pay-7e10', amt: 189, ord: 'ord-3381'},
  {pay: 'pay-7f21', amt: 249, ord: null},
  {pay: 'pay-7f3a', amt: 412, ord: 'ord-3383'},
  {pay: 'pay-7f4c', amt: 96, ord: null},
  {pay: 'pay-7f58', amt: 305, ord: null},
  {pay: 'pay-7f6d', amt: 158, ord: 'ord-3386'},
];

const TRACE = [
  {who: 'PSP', what: 'captured 249 SAR', ok: true},
  {who: 'gateway', what: 'told the customer “ord-338b”', ok: true},
  {who: 'orders', what: '“queued for batch insert”', ok: true, warn: true},
  {who: 'Postgres', what: 'no order row', ok: false},
];

const B = 150; // trace phase
const R = 252; // repair phase

const Scale: React.FC<{tilt: number}> = ({tilt}) => {
  const L = 250;
  const th = (tilt * Math.PI) / 180;
  const left = {x: -L * Math.cos(th), y: -L * Math.sin(th)};
  const right = {x: L * Math.cos(th), y: L * Math.sin(th)};
  const pan = (p: {x: number; y: number}, label: string, col: string) => (
    <g>
      <line x1={p.x} y1={p.y} x2={p.x - 90} y2={p.y + 170} stroke={C.gold} strokeOpacity={0.6} strokeWidth={2} />
      <line x1={p.x} y1={p.y} x2={p.x + 90} y2={p.y + 170} stroke={C.gold} strokeOpacity={0.6} strokeWidth={2} />
      <path
        d={`M ${p.x - 110} ${p.y + 170} Q ${p.x} ${p.y + 240} ${p.x + 110} ${p.y + 170} Z`}
        fill="rgba(240,189,85,0.12)"
        stroke={C.gold}
        strokeWidth={3}
      />
      <text
        x={p.x}
        y={p.y + 262}
        textAnchor="middle"
        fontFamily={FONT.mono}
        fontSize={24}
        fill={col}
      >
        {label}
      </text>
    </g>
  );
  return (
    <svg width={760} height={560} viewBox="-380 -120 760 560" style={{overflow: 'visible'}}>
      <defs>
        <linearGradient id="pillar" x1="0" x2="0" y1="0" y2="1">
          <stop offset="0" stopColor={C.gold} />
          <stop offset="1" stopColor="rgba(240,189,85,0.2)" />
        </linearGradient>
      </defs>
      <rect x={-5} y={0} width={10} height={380} fill="url(#pillar)" />
      <path d="M -120 400 L 120 400 L 80 380 L -80 380 Z" fill={C.gold} opacity={0.6} />
      <line
        x1={left.x}
        y1={left.y}
        x2={right.x}
        y2={right.y}
        stroke={C.gold}
        strokeWidth={8}
        strokeLinecap="round"
        style={{filter: `drop-shadow(0 0 12px rgba(240,189,85,0.6))`}}
      />
      <circle r={18} fill={C.gold} />
      <circle r={7} fill={C.bg} />
      {pan(left, 'PSP ledger', C.gold)}
      {pan(right, 'orders', C.ivory)}
    </svg>
  );
};

export const MizanScene: React.FC = () => {
  const frame = useCurrentFrame();
  const {fps} = useVideoConfig();

  const imbalance = sp(frame, fps, 70, {damping: 14, stiffness: 60});
  const balanced = sp(frame, fps, R + 50, {damping: 7, stiffness: 50});
  const tilt = -13 * imbalance * (1 - balanced);

  const ledgerOut = interpolate(frame, [B - 10, B + 4], [1, 0], clamp);
  const traceOut = interpolate(frame, [R - 10, R + 4], [1, 0], clamp);
  const lost = interpolate(frame, [80, 140], [0, 14193], clamp);
  const rebuilt = interpolate(frame, [R + 20, R + 70], [0, 54], clamp);

  return (
    <AbsoluteFill>
      <Backdrop glow="rgba(240,189,85,0.14)" glow2="rgba(90,70,20,0.35)" />

      <div style={{position: 'absolute', left: 150, top: 96, width: 1600}}>
        <Kicker color={C.gold}>Mizan · ميزان</Kicker>
        <div style={{height: 18}} />
        <Headline
          text="Failures that never raise an alert."
          delay={6}
          size={70}
          highlight={{never: C.gold}}
        />
      </div>

      {/* Scale */}
      <div style={{position: 'absolute', left: 1110, top: 320, ...rise(frame, fps, 10, 60)}}>
        <Scale tilt={tilt} />
      </div>
      <div
        style={{
          position: 'absolute',
          left: 1110,
          width: 760,
          top: 880,
          textAlign: 'center',
          fontFamily: FONT.sans,
          ...rise(frame, fps, 30, 20),
        }}
      >
        {frame < R + 50 ? (
          <div style={{fontSize: 30, color: C.muted}}>
            <span style={{color: C.crit, fontWeight: 700}}>{fmt(lost)} SAR</span> paid · no order ·{' '}
            <span style={{color: C.ivory}}>0 alerts</span>
          </div>
        ) : (
          <div
            style={{
              fontSize: 54,
              fontWeight: 800,
              color: C.gold,
              textShadow: '0 0 40px rgba(240,189,85,0.5)',
              transform: `scale(${sp(frame, fps, R + 50, BOUNCY)})`,
            }}
          >
            The books balance ✓
          </div>
        )}
      </div>

      {/* Phase A: two ledgers that don't agree */}
      <div style={{position: 'absolute', left: 150, top: 330, width: 900, opacity: ledgerOut, display: 'flex', gap: 28}}>
        {(['psp', 'orders'] as const).map((col, ci) => (
          <Glass key={col} style={{flex: 1, padding: 28, ...rise(frame, fps, 14 + ci * 8, 40)}}>
            <div style={{fontFamily: FONT.mono, fontSize: 20, letterSpacing: 3, color: ci === 0 ? C.gold : C.ivory}}>
              {ci === 0 ? 'PSP SETTLEMENT LEDGER' : 'ORDERS TABLE'}
            </div>
            <div style={{display: 'flex', flexDirection: 'column', gap: 10, marginTop: 20}}>
              {ROWS.map((r, i) => {
                const s = sp(frame, fps, 24 + i * 6 + ci * 4, SNAPPY);
                const missing = ci === 1 && !r.ord;
                const blink = missing && frame > 80 ? 0.55 + 0.45 * Math.abs(Math.sin(frame / 5)) : 1;
                return (
                  <div
                    key={r.pay}
                    style={{
                      opacity: s * blink,
                      transform: `translateX(${(1 - s) * 30}px)`,
                      display: 'flex',
                      justifyContent: 'space-between',
                      padding: '12px 16px',
                      borderRadius: 12,
                      fontFamily: FONT.mono,
                      fontSize: 22,
                      background: missing ? C.critSoft : 'rgba(242,243,239,0.04)',
                      border: missing ? `1px dashed ${C.crit}` : '1px solid transparent',
                      color: missing ? C.crit : C.ivory,
                    }}
                  >
                    {ci === 0 ? (
                      <>
                        <span>{r.pay}</span>
                        <span style={{color: C.gold}}>{r.amt} SAR</span>
                      </>
                    ) : missing ? (
                      <span>— missing —</span>
                    ) : (
                      <>
                        <span>{r.ord}</span>
                        <span style={{color: C.muted}}>{r.pay}</span>
                      </>
                    )}
                  </div>
                );
              })}
            </div>
          </Glass>
        ))}
      </div>

      {/* Phase B: trace one lost payment */}
      {frame >= B - 6 && (
        <div style={{position: 'absolute', left: 150, top: 320, width: 900, opacity: traceOut}}>
          <div style={{fontFamily: FONT.mono, fontSize: 24, color: C.gold, ...rise(frame, fps, B, 20)}}>
            trace_item(<span style={{color: C.ivory}}>pay-7f21</span>)
          </div>
          <div style={{position: 'relative', marginTop: 22}}>
            <div
              style={{
                position: 'absolute',
                left: 23,
                top: 30,
                width: 3,
                height: interpolate(frame, [B + 6, B + 50], [0, 3 * 92], clamp),
                background: `linear-gradient(${C.gold}, ${C.crit})`,
              }}
            />
            {TRACE.map((t, i) => {
              const s = sp(frame, fps, B + 8 + i * 12, SNAPPY);
              const col = t.ok ? (t.warn ? C.warn : C.gold) : C.crit;
              return (
                <div
                  key={t.who}
                  style={{
                    display: 'flex',
                    alignItems: 'center',
                    gap: 26,
                    height: 92,
                    opacity: s,
                    transform: `translateX(${(1 - s) * 40}px)`,
                  }}
                >
                  <div
                    style={{
                      width: 50,
                      height: 50,
                      borderRadius: '50%',
                      border: `3px solid ${col}`,
                      background: t.ok ? C.bg : C.critSoft,
                      display: 'flex',
                      alignItems: 'center',
                      justifyContent: 'center',
                      color: col,
                      fontSize: 24,
                      fontWeight: 800,
                      boxShadow: `0 0 20px ${col}`,
                      position: 'relative',
                      zIndex: 1,
                    }}
                  >
                    {t.ok ? '✓' : '✕'}
                  </div>
                  <span style={{fontFamily: FONT.mono, fontSize: 26, color: col, width: 160}}>{t.who}</span>
                  <span style={{fontFamily: FONT.sans, fontSize: 30, color: t.ok ? C.ivory : C.crit}}>{t.what}</span>
                </div>
              );
            })}
          </div>
          <Glass
            style={{
              marginTop: 26,
              padding: '20px 26px',
              ...rise(frame, fps, B + 62, 20),
              display: 'flex',
              flexDirection: 'column',
              gap: 8,
            }}
            tint="rgba(155,123,255,0.08)"
          >
            <span style={{fontFamily: FONT.sans, fontSize: 26, color: C.ivory}}>
              Suspect: <b>PR #511</b> · orders <span style={{fontFamily: FONT.mono, color: C.warn}}>write_mode: batched</span>
            </span>
            <span style={{fontFamily: FONT.sans, fontSize: 24, color: C.violet}}>
              Skeptic: first loss came 2 s after that deploy — upheld ✓
            </span>
          </Glass>
        </div>
      )}

      {/* Phase C: fix the cause, then repair the data */}
      {frame >= R - 6 && (
        <div style={{position: 'absolute', left: 150, top: 330, width: 900}}>
          {[
            {k: '1', t: 'Fix the cause', d: 'roll back orders config · v2 → v1', at: R},
            {k: '2', t: 'Repair the data', d: 'rebuild every promised order from the PSP ledger', at: R + 16},
            {k: '3', t: 'Prove it', d: 'payments = orders = fulfillments', at: R + 50},
          ].map((s) => (
            <Glass
              key={s.k}
              style={{
                padding: '26px 30px',
                marginBottom: 20,
                display: 'flex',
                gap: 26,
                alignItems: 'center',
                ...rise(frame, fps, s.at, 30),
              }}
            >
              <span
                style={{
                  width: 58,
                  height: 58,
                  borderRadius: 16,
                  background: C.goldSoft,
                  border: `2px solid ${C.gold}`,
                  color: C.gold,
                  fontFamily: FONT.sans,
                  fontWeight: 800,
                  fontSize: 28,
                  display: 'flex',
                  alignItems: 'center',
                  justifyContent: 'center',
                  flexShrink: 0,
                }}
              >
                {s.k}
              </span>
              <div style={{flex: 1}}>
                <div style={{fontFamily: FONT.sans, fontSize: 34, fontWeight: 700, color: C.ivory}}>{s.t}</div>
                <div style={{fontFamily: FONT.mono, fontSize: 21, color: C.muted, marginTop: 4}}>{s.d}</div>
                {s.k === '2' && (
                  <div style={{marginTop: 14, height: 10, borderRadius: 6, background: 'rgba(242,243,239,0.08)'}}>
                    <div
                      style={{
                        width: `${(rebuilt / 54) * 100}%`,
                        height: '100%',
                        borderRadius: 6,
                        background: `linear-gradient(90deg, ${C.gold}, #ffe3a3)`,
                        boxShadow: '0 0 16px rgba(240,189,85,0.7)',
                      }}
                    />
                  </div>
                )}
              </div>
              <span style={{fontFamily: FONT.mono, fontSize: 24, color: C.gold, minWidth: 130, textAlign: 'right'}}>
                {s.k === '2' ? `${Math.round(rebuilt)} / 54` : frame >= s.at + 14 ? '✓' : ''}
              </span>
            </Glass>
          ))}
        </div>
      )}
    </AbsoluteFill>
  );
};
