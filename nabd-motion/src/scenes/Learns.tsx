import React from 'react';
import {AbsoluteFill, interpolate, useCurrentFrame, useVideoConfig} from 'remotion';
import {Backdrop, Glass, Headline, Kicker} from '../ui';
import {BOUNCY, C, clamp, FONT, rise, sp, SNAPPY} from '../theme';

// Beats, in frames
const LEARNED = 96; // four minutes of healthy traffic, compressed
const SALE_IN = 128;
const SALE_OUT = 196;
const BREAK = 222; // stock stops going down
const CAUGHT = 262; // "detected in 18 s"

const WAVE_X = 450;
const WAVE_W = 1010;
const SAMPLES = 130;
const LANE_A = 470;
const LANE_B = 690;

/** Shopper traffic: bursty, deterministic, the same for both flows (that is the law). */
const traffic = (t: number) => {
  const v =
    0.5 +
    0.28 * Math.sin(t * 0.21) +
    0.16 * Math.sin(t * 0.67 + 1.3) +
    0.12 * Math.sin(t * 1.37 + 0.4) +
    (Math.sin(t * 0.093) > 0.7 ? 0.35 : 0);
  return Math.max(0.05, v);
};

const load = (frame: number) =>
  1 + 3.2 * interpolate(frame, [SALE_IN, SALE_IN + 18, SALE_OUT - 10, SALE_OUT + 8], [0, 1, 1, 0], clamp);

const wavePath = (frame: number, laneY: number, flatAfter: number | null) => {
  const pts: string[] = [];
  for (let i = 0; i <= SAMPLES; i++) {
    const t = frame - (SAMPLES - i) * 0.9; // the sample's own moment: the line scrolls left
    const x = WAVE_X + (i / SAMPLES) * WAVE_W;
    const dead = flatAfter !== null && t > flatAfter;
    const h = dead ? 0 : traffic(t * 0.5) * 34 * load(t);
    pts.push(`${i === 0 ? 'M' : 'L'} ${x.toFixed(1)} ${(laneY - h).toFixed(1)}`);
  }
  return pts.join(' ');
};

const Lane: React.FC<{y: number; label: string; sub: string; color: string; frame: number; flatAfter: number | null; reveal: number}> = ({
  y,
  label,
  sub,
  color,
  frame,
  flatAfter,
  reveal,
}) => {
  const dead = flatAfter !== null && frame > flatAfter + 6;
  const stroke = dead ? C.crit : color;
  return (
    <>
      <div style={{position: 'absolute', left: 186, top: y - 70, width: 250, opacity: reveal}}>
        <div style={{fontFamily: FONT.mono, fontSize: 20, color: stroke, whiteSpace: 'nowrap'}}>{label}</div>
        <div style={{fontFamily: FONT.sans, fontSize: 21, color: C.muted, marginTop: 6, lineHeight: 1.3}}>{sub}</div>
      </div>
      <svg width={1920} height={1080} style={{position: 'absolute', left: 0, top: 0, opacity: reveal}}>
        <defs>
          <clipPath id={`reveal-${y}`}>
            <rect x={WAVE_X} y={y - 220} width={WAVE_W * reveal} height={240} />
          </clipPath>
        </defs>
        <line x1={WAVE_X} y1={y} x2={WAVE_X + WAVE_W} y2={y} stroke={C.line} strokeWidth={2} />
        <path
          d={wavePath(frame, y, flatAfter)}
          fill="none"
          stroke={stroke}
          strokeWidth={4}
          strokeLinejoin="round"
          clipPath={`url(#reveal-${y})`}
          style={{filter: `drop-shadow(0 0 10px ${stroke})`}}
        />
        <circle cx={WAVE_X + WAVE_W} cy={y - (flatAfter !== null && frame > flatAfter ? 0 : traffic(frame * 0.5) * 34 * load(frame))} r={8} fill={stroke} />
      </svg>
    </>
  );
};

export const LearnsScene: React.FC = () => {
  const frame = useCurrentFrame();
  const {fps} = useVideoConfig();

  const reveal = interpolate(frame, [20, 70], [0, 1], {...clamp, easing: (t) => 1 - Math.pow(1 - t, 3)});
  const learnProgress = interpolate(frame, [24, LEARNED], [0, 1], clamp);
  const learned = sp(frame, fps, LEARNED, BOUNCY);
  const sale = interpolate(frame, [SALE_IN, SALE_IN + 12, SALE_OUT - 6, SALE_OUT + 6], [0, 1, 1, 0], clamp);
  const broken = frame > BREAK + 6;
  const caught = sp(frame, fps, CAUGHT, SNAPPY);
  const lawCount = Math.round(interpolate(frame, [LEARNED - 10, LEARNED + 26], [0, 129], clamp));

  const chipColor = broken ? C.crit : C.gold;
  const ratio = broken ? '1.00 → 0.00' : '1.00 : 1.00';
  const chipLine = broken ? 'law broken' : sale > 0.5 ? 'holds at 5× load' : 'law holds';

  return (
    <AbsoluteFill>
      <Backdrop glow="rgba(240,189,85,0.12)" glow2="rgba(15,107,82,0.35)" />

      <div style={{position: 'absolute', left: 150, top: 96, width: 1150}}>
        <Kicker color={C.gold}>Mizan learns · laws nobody wrote</Kicker>
        <div style={{height: 18}} />
        <Headline text="It learns your laws by itself." delay={4} size={70} highlight={{itself: C.gold}} />
      </div>

      {/* Laws learned counter */}
      <div
        style={{
          position: 'absolute',
          right: 150,
          top: 92,
          textAlign: 'right',
          fontFamily: FONT.sans,
          opacity: interpolate(frame, [20, 34], [0, 1], clamp),
        }}
      >
        <div style={{fontFamily: FONT.mono, fontSize: 19, letterSpacing: 3, color: C.gold}}>
          {learnProgress < 1 ? `LEARNING FROM HEALTHY TRAFFIC ${Math.round(learnProgress * 100)}%` : 'LEARNED IN 4 MINUTES'}
        </div>
        <div
          style={{
            fontSize: 112,
            fontWeight: 800,
            lineHeight: 1,
            letterSpacing: -3,
            color: C.ivory,
            fontVariantNumeric: 'tabular-nums',
            transform: `scale(${0.9 + 0.1 * learned})`,
            transformOrigin: 'right',
            textShadow: '0 0 40px rgba(240,189,85,0.35)',
          }}
        >
          {lawCount}
          <span style={{fontSize: 40, color: C.gold, letterSpacing: 0}}> laws</span>
        </div>
        <div style={{fontSize: 24, color: C.muted, marginTop: 4}}>from 21 flows · no rule written by hand</div>
      </div>

      {/* The scope */}
      <Glass style={{position: 'absolute', left: 150, top: 300, width: 1620, height: 500, ...rise(frame, fps, 12, 50)}}>
        <div />
      </Glass>
      <Lane
        y={LANE_A}
        label="inventory /reserve"
        sub="every reservation"
        color={C.accent}
        frame={frame}
        flatAfter={null}
        reveal={reveal}
      />
      <Lane
        y={LANE_B}
        label="products update"
        sub="the stock going down"
        color={C.gold}
        frame={frame}
        flatAfter={BREAK}
        reveal={reveal}
      />

      {/* Flash-sale badge */}
      <div
        style={{
          position: 'absolute',
          left: WAVE_X + 30,
          top: 322,
          opacity: sale,
          transform: `translateY(${(1 - sale) * -16}px)`,
          fontFamily: FONT.mono,
          fontSize: 21,
          letterSpacing: 2,
          color: C.warn,
          padding: '8px 16px',
          border: `1.5px solid ${C.warn}`,
          borderRadius: 999,
          background: 'rgba(255,176,32,0.08)',
        }}
      >
        FLASH SALE · 5× SHOPPERS · NOTHING BROKEN
      </div>

      {/* The law between the two flows */}
      <div
        style={{
          position: 'absolute',
          left: 1500,
          top: (LANE_A + LANE_B) / 2 - 58,
          width: 250,
          padding: '14px 22px',
          borderRadius: 18,
          border: `2px solid ${chipColor}`,
          background: broken ? 'rgba(255,61,90,0.12)' : 'rgba(240,189,85,0.08)',
          boxShadow: `0 0 ${broken ? 50 : 24}px ${chipColor}55`,
          opacity: learned,
          transform: `scale(${0.85 + 0.15 * learned + (broken ? 0.06 * Math.sin(frame / 3) * Math.max(0, 1 - (frame - BREAK) / 40) : 0)})`,
          fontFamily: FONT.sans,
        }}
      >
        <div style={{fontFamily: FONT.mono, fontSize: 28, fontWeight: 700, color: chipColor, whiteSpace: 'nowrap'}}>{ratio}</div>
        <div style={{fontSize: 21, color: broken ? '#ffc2cb' : C.muted, marginTop: 4}}>{chipLine}</div>
      </div>

      {/* Below the scope: what it means */}
      <div
        style={{
          position: 'absolute',
          left: 150,
          top: 852,
          width: 1620,
          display: 'flex',
          justifyContent: 'space-between',
          opacity: 1 - caught,
        }}
      >
        {['Ratios survive load: 5× sale, 0 false alarms', 'Works on any system with metrics', 'Jiang et al. 2006 · ACI 2021'].map(
          (f, i) => (
            <div
              key={f}
              style={{...rise(frame, fps, 60 + i * 10, 20), fontFamily: FONT.sans, fontSize: 25, color: C.ivory, display: 'flex', gap: 14, alignItems: 'center', whiteSpace: 'nowrap'}}
            >
              <span style={{color: C.gold, fontSize: 20}}>◆</span>
              {f}
            </div>
          ),
        )}
      </div>

      {frame >= CAUGHT && (
        <Glass
          tint="rgba(255,61,90,0.10)"
          style={{
            position: 'absolute',
            left: 150,
            top: 830,
            width: 1620,
            padding: '22px 36px',
            border: `1.5px solid ${C.crit}`,
            display: 'flex',
            alignItems: 'center',
            gap: 40,
            opacity: caught,
            transform: `translateY(${(1 - caught) * 40}px)`,
          }}
        >
          <div style={{fontFamily: FONT.sans}}>
            <div style={{fontSize: 34, fontWeight: 800, color: C.ivory}}>Stock silently stopped going down.</div>
            <div style={{fontSize: 23, color: C.muted, marginTop: 4}}>No rule was ever written for stock. Every order still succeeded.</div>
          </div>
          <div style={{marginLeft: 'auto', textAlign: 'right', fontFamily: FONT.sans}}>
            <div style={{fontSize: 34, fontWeight: 800, color: C.crit}}>Caught in 18 s</div>
            <div style={{fontSize: 23, color: C.muted, marginTop: 4}}>
              suspect <span style={{fontFamily: FONT.mono, color: C.ivory}}>inventory</span>: changed 20 s before
            </div>
          </div>
        </Glass>
      )}
    </AbsoluteFill>
  );
};
