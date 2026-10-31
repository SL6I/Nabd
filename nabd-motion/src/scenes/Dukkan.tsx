import React from 'react';
import {AbsoluteFill, interpolate, useCurrentFrame, useVideoConfig} from 'remotion';
import {Backdrop, Dot, Glass, Headline, Kicker} from '../ui';
import {BOUNCY, C, clamp, FONT, rise, sp, SNAPPY} from '../theme';

type Status = 'ok' | 'warn' | 'crit' | 'muted' | 'fixed';
type NodeDef = {id: string; label: string; x: number; y: number; ext?: boolean};

const NODES: NodeDef[] = [
  {id: 'shoppers', label: 'Shoppers', x: 160, y: 650, ext: true},
  {id: 'gateway', label: 'gateway', x: 400, y: 650},
  {id: 'auth', label: 'auth', x: 660, y: 360},
  {id: 'inventory', label: 'inventory', x: 660, y: 550},
  {id: 'orders', label: 'orders', x: 660, y: 750},
  {id: 'payments', label: 'payments', x: 660, y: 940},
  {id: 'redis', label: 'Redis', x: 930, y: 455},
  {id: 'catalog', label: 'catalog-sync', x: 1150, y: 560},
  {id: 'postgres', label: 'Postgres', x: 930, y: 700},
  {id: 'queue', label: 'order queue', x: 900, y: 850},
  {id: 'fulfill', label: 'fulfillment', x: 1170, y: 850},
  {id: 'psp', label: 'PSP', x: 930, y: 990, ext: true},
];
const byId = Object.fromEntries(NODES.map((n) => [n.id, n]));

const EDGES: [string, string][] = [
  ['shoppers', 'gateway'],
  ['gateway', 'auth'],
  ['gateway', 'inventory'],
  ['gateway', 'orders'],
  ['gateway', 'payments'],
  ['auth', 'redis'],
  ['inventory', 'redis'],
  ['orders', 'postgres'],
  ['catalog', 'postgres'],
  ['orders', 'queue'],
  ['queue', 'fulfill'],
  ['payments', 'psp'],
];

export const STORM = 190;
const TRIAGE = 300;
const FIX = 362;

const ALERTS = [
  ['RedisMemoryFull', 'redis'],
  ['PaymentsPSPTimeouts', 'payments'],
  ['OrdersDBLatencyHigh', 'orders'],
  ['GatewayErrorRate', 'gateway'],
  ['CheckoutSLOBurn', 'gateway'],
  ['AuthSessionErrors', 'auth'],
  ['DeployDetected', 'inventory'],
];

const ROOTS = [
  {n: 1, title: 'Cache key leak fills Redis', sub: 'tmp:session-dump:* · no TTL', fix: 'keys flushed', at: FIX, node: 'redis'},
  {n: 2, title: 'Degraded DB link', sub: 'orders → Postgres +700 ms', fix: 'failed over', at: FIX + 14, node: 'postgres'},
  {n: 3, title: 'Bad config deploy', sub: 'payments v2 · timeout 2000 → 25 ms', fix: 'needs a human', at: FIX + 28, node: 'payments'},
];

const nodeStatus = (id: string, f: number): Status => {
  switch (id) {
    case 'redis':
      return f >= FIX ? 'fixed' : f >= STORM + 6 ? 'crit' : 'ok';
    case 'payments':
      return f >= STORM + 14 ? 'crit' : 'ok';
    case 'inventory':
      return f >= TRIAGE + 20 ? 'muted' : f >= STORM ? 'warn' : 'ok';
    case 'gateway':
    case 'auth':
    case 'orders':
      return f >= STORM + 26 ? 'warn' : 'ok';
    default:
      return 'ok';
  }
};

const edgeStatus = (a: string, b: string, f: number): Status => {
  if (a === 'orders' && b === 'postgres') return f >= FIX + 14 ? 'fixed' : f >= STORM + 20 ? 'crit' : 'ok';
  if (a === 'payments' && b === 'psp') return f >= STORM + 14 ? 'crit' : 'ok';
  if (b === 'redis') return f >= FIX ? 'ok' : f >= STORM + 6 ? 'crit' : 'ok';
  return 'ok';
};

const color = (s: Status) =>
  s === 'crit' ? C.crit : s === 'warn' ? C.warn : s === 'muted' ? 'rgba(242,243,239,0.28)' : C.accent;

const Topology: React.FC = () => {
  const frame = useCurrentFrame();
  const {fps} = useVideoConfig();

  return (
    <AbsoluteFill>
      <svg width={1920} height={1080} style={{position: 'absolute'}}>
        {EDGES.map(([a, b], i) => {
          const A = byId[a];
          const B = byId[b];
          const st = edgeStatus(a, b, frame);
          const col = color(st);
          const reveal = interpolate(frame, [18 + i * 4, 44 + i * 4], [0, 1], clamp);
          const len = Math.hypot(B.x - A.x, B.y - A.y);
          const speed = st === 'crit' ? 0.9 : 3.2;
          return (
            <g key={`${a}-${b}`}>
              <line
                x1={A.x}
                y1={A.y}
                x2={A.x + (B.x - A.x) * reveal}
                y2={A.y + (B.y - A.y) * reveal}
                stroke={col}
                strokeOpacity={st === 'ok' ? 0.3 : 0.85}
                strokeWidth={st === 'crit' ? 4 : 2.5}
                strokeDasharray={st === 'crit' ? '12 10' : undefined}
                strokeDashoffset={-frame * 1.5}
              />
              {reveal >= 1 &&
                [0, 0.5].map((k) => {
                  const t = ((frame * speed) / len + k + i * 0.13) % 1;
                  return (
                    <circle
                      key={k}
                      cx={A.x + (B.x - A.x) * t}
                      cy={A.y + (B.y - A.y) * t}
                      r={st === 'crit' ? 6 : 5}
                      fill={col}
                      style={{filter: `drop-shadow(0 0 8px ${col})`}}
                    />
                  );
                })}
            </g>
          );
        })}
      </svg>

      {/* DB-link latency tag */}
      {frame >= STORM + 20 && (
        <div
          style={{
            position: 'absolute',
            left: 760,
            top: 664,
            fontFamily: FONT.mono,
            fontSize: 20,
            color: frame >= FIX + 14 ? C.accent : C.crit,
            background: C.bg,
            padding: '2px 10px',
            borderRadius: 8,
            transform: `scale(${sp(frame, fps, STORM + 20, BOUNCY)})`,
          }}
        >
          {frame >= FIX + 14 ? '9 ms ✓' : '+700 ms'}
        </div>
      )}

      {NODES.map((n, i) => {
        const s = sp(frame, fps, 6 + i * 4, BOUNCY);
        const st = nodeStatus(n.id, frame);
        const col = color(st);
        const pulse = st === 'crit' ? (Math.sin(frame / 3.5) + 1) / 2 : 0;
        const root = ROOTS.find((r) => r.node === n.id);
        const badge = root ? sp(frame, fps, TRIAGE + 10 + root.n * 6, BOUNCY) : 0;
        return (
          <div
            key={n.id}
            style={{
              position: 'absolute',
              left: n.x,
              top: n.y,
              transform: `translate(-50%, -50%) scale(${s})`,
              display: 'flex',
              alignItems: 'center',
              gap: 12,
              padding: '14px 24px',
              borderRadius: n.ext ? 999 : 18,
              background: st === 'crit' ? 'rgba(60,8,16,0.92)' : 'rgba(6,26,20,0.92)',
              border: `2px ${n.ext ? 'dashed' : 'solid'} ${col}`,
              boxShadow: `0 0 ${18 + pulse * 40}px ${st === 'ok' ? 'rgba(46,230,166,0.18)' : col}`,
              fontFamily: FONT.mono,
              fontSize: 24,
              color: st === 'muted' ? C.faint : C.ivory,
              textDecoration: st === 'muted' ? 'line-through' : undefined,
              whiteSpace: 'nowrap',
            }}
          >
            <Dot color={col} size={12} pulse={pulse} />
            {n.label}
            {root && badge > 0.01 && (
              <span
                style={{
                  position: 'absolute',
                  top: -20,
                  right: -20,
                  width: 40,
                  height: 40,
                  borderRadius: '50%',
                  background: st === 'fixed' ? C.accent : C.crit,
                  color: C.ink,
                  fontFamily: FONT.sans,
                  fontWeight: 800,
                  fontSize: 22,
                  display: 'flex',
                  alignItems: 'center',
                  justifyContent: 'center',
                  transform: `scale(${badge})`,
                  textDecoration: 'none',
                }}
              >
                {frame >= root.at && root.n !== 3 ? '✓' : root.n}
              </span>
            )}
          </div>
        );
      })}
    </AbsoluteFill>
  );
};

const SidePanel: React.FC = () => {
  const frame = useCurrentFrame();
  const {fps} = useVideoConfig();
  const panelIn = sp(frame, fps, STORM - 4, SNAPPY);
  const alertsOut = interpolate(frame, [TRIAGE - 6, TRIAGE + 8], [1, 0], clamp);
  const visible = ALERTS.filter((_, i) => frame >= STORM + 4 + i * 9).length;

  return (
    <Glass
      style={{
        position: 'absolute',
        left: 1300,
        top: 300,
        width: 520,
        height: 720,
        padding: 34,
        opacity: panelIn,
        transform: `translateX(${(1 - panelIn) * 120}px)`,
        overflow: 'hidden',
      }}
      tint={frame < TRIAGE ? 'rgba(255,61,90,0.07)' : 'rgba(46,230,166,0.06)'}
    >
      {/* Alert storm */}
      <div style={{position: 'absolute', inset: 34, opacity: alertsOut}}>
        <div style={{display: 'flex', justifyContent: 'space-between', alignItems: 'baseline'}}>
          <span style={{fontFamily: FONT.sans, fontSize: 32, fontWeight: 700, color: C.ivory}}>Alert storm</span>
          <span style={{fontFamily: FONT.mono, fontSize: 44, fontWeight: 700, color: C.crit}}>{visible}</span>
        </div>
        <div style={{display: 'flex', flexDirection: 'column', gap: 12, marginTop: 24}}>
          {ALERTS.map(([name, svc], i) => {
            const s = sp(frame, fps, STORM + 4 + i * 9, SNAPPY);
            return (
              <div
                key={name}
                style={{
                  opacity: s,
                  transform: `translateX(${(1 - s) * 80}px)`,
                  display: 'flex',
                  alignItems: 'center',
                  gap: 14,
                  padding: '13px 18px',
                  borderRadius: 14,
                  background: C.critSoft,
                  borderLeft: `4px solid ${svc === 'inventory' ? C.warn : C.crit}`,
                }}
              >
                <span style={{fontFamily: FONT.mono, fontSize: 21, color: C.ivory, flex: 1}}>{name}</span>
                <span style={{fontFamily: FONT.mono, fontSize: 18, color: C.muted}}>{svc}</span>
              </div>
            );
          })}
        </div>
      </div>

      {/* Triage result */}
      <div style={{position: 'absolute', inset: 34, opacity: 1 - alertsOut}}>
        <div style={{fontFamily: FONT.mono, fontSize: 20, letterSpacing: 4, color: C.accent}}>NABD · TRIAGE</div>
        <div style={{fontFamily: FONT.sans, fontSize: 34, fontWeight: 700, color: C.ivory, marginTop: 8}}>
          7 alerts → 3 root causes
        </div>
        <div style={{display: 'flex', flexDirection: 'column', gap: 14, marginTop: 26}}>
          {ROOTS.map((r, i) => {
            const s = sp(frame, fps, TRIAGE + 8 + i * 7, SNAPPY);
            const done = frame >= r.at;
            const human = r.n === 3 && done;
            const chip = human ? C.warn : done ? C.accent : C.crit;
            return (
              <div
                key={r.n}
                style={{
                  opacity: s,
                  transform: `translateY(${(1 - s) * 30}px)`,
                  padding: '16px 18px',
                  borderRadius: 16,
                  background: 'rgba(242,243,239,0.04)',
                  border: `1px solid ${C.line}`,
                }}
              >
                <div style={{display: 'flex', alignItems: 'center', gap: 12}}>
                  <span style={{fontFamily: FONT.sans, fontWeight: 800, fontSize: 22, color: C.crit}}>{r.n}</span>
                  <span style={{fontFamily: FONT.sans, fontSize: 24, fontWeight: 600, color: C.ivory, flex: 1}}>
                    {r.title}
                  </span>
                </div>
                <div style={{display: 'flex', justifyContent: 'space-between', marginTop: 6}}>
                  <span style={{fontFamily: FONT.mono, fontSize: 17, color: C.muted}}>{r.sub}</span>
                  {done && (
                    <span
                      style={{
                        fontFamily: FONT.mono,
                        fontSize: 16,
                        color: chip,
                        transform: `scale(${sp(frame, fps, r.at, BOUNCY)})`,
                      }}
                    >
                      {human ? '⏳ ' : '✓ '}
                      {r.fix}
                    </span>
                  )}
                </div>
              </div>
            );
          })}
          <div
            style={{
              ...rise(frame, fps, TRIAGE + 30, 20),
              padding: '14px 18px',
              borderRadius: 16,
              border: `1px dashed ${C.faint}`,
              fontFamily: FONT.sans,
              fontSize: 21,
              color: C.faint,
            }}
          >
            <span style={{textDecoration: 'line-through'}}>inventory v1.8 deploy</span>
            <span style={{color: C.muted}}> — red herring, ruled out</span>
          </div>
        </div>
      </div>
    </Glass>
  );
};

export const DukkanScene: React.FC = () => {
  const frame = useCurrentFrame();
  const {fps} = useVideoConfig();
  const t1 = interpolate(frame, [STORM - 14, STORM], [1, 0], clamp);
  const shake = frame >= STORM && frame < STORM + 10 ? Math.sin(frame * 9) * (STORM + 10 - frame) * 1.6 : 0;
  const flash = interpolate(frame, [STORM, STORM + 3, STORM + 22], [0, 0.22, 0], clamp);

  return (
    <AbsoluteFill>
      <Backdrop glow={frame >= STORM && frame < FIX ? 'rgba(255,61,90,0.14)' : undefined} />
      <AbsoluteFill style={{transform: `translateX(${shake}px)`}}>
        <Topology />
        <SidePanel />
      </AbsoluteFill>

      <div style={{position: 'absolute', left: 150, top: 96}}>
        <div style={{opacity: t1, position: 'absolute', width: 1600}}>
          <Kicker>Meet Dukkan · دكّان</Kicker>
          <div style={{height: 18}} />
          <Headline
            text="A live Saudi test store. Simulated shoppers."
            delay={10}
            size={68}
            highlight={{Simulated: C.gold}}
          />
        </div>
        {frame >= STORM - 6 && (
          <div style={{position: 'absolute', width: 1600}}>
            <Kicker color={C.crit} delay={STORM - 6}>
              Perfect storm
            </Kicker>
            <div style={{height: 18}} />
            <Headline
              text={frame < TRIAGE ? 'Three faults. At once.' : 'Nabd finds every root cause.'}
              key={frame < TRIAGE ? 'a' : 'b'}
              delay={frame < TRIAGE ? STORM : TRIAGE}
              size={68}
              highlight={{Three: C.crit, every: C.accent}}
            />
          </div>
        )}
      </div>

      <AbsoluteFill style={{background: C.crit, opacity: flash, mixBlendMode: 'screen'}} />
    </AbsoluteFill>
  );
};
