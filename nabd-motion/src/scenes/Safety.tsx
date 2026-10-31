import React from 'react';
import {AbsoluteFill, Easing, interpolate, useCurrentFrame, useVideoConfig} from 'remotion';
import {Backdrop, Glass, Headline, Kicker} from '../ui';
import {BOUNCY, C, clamp, FONT, rise, sp} from '../theme';

const GATES = [
  {name: 'Safety gate', detail: 'catalog actions only', x: 640},
  {name: 'Validity gate', detail: 'does it undo the cause?', x: 920},
  {name: 'Dry run', detail: 'timeout_ms 25 → 2000', x: 1200},
  {name: 'Risk · blast radius', detail: 'HIGH → a human decides', x: 1480, warn: true},
];
const T = [30, 62, 94, 126, 158, 186];
const Y = 540;
const CALL = 196;
const OK = 252;

export const SafetyScene: React.FC = () => {
  const frame = useCurrentFrame();
  const {fps} = useVideoConfig();

  const cardX = interpolate(frame, T, [130, GATES[0].x, GATES[1].x, GATES[2].x, GATES[3].x, 1720], {
    ...clamp,
    easing: Easing.inOut(Easing.cubic),
  });
  const cardIn = sp(frame, fps, 14, BOUNCY);
  const flagged = frame >= T[4];
  const approved = frame >= OK;
  const cardColor = approved ? C.accent : flagged ? C.warn : C.violet;

  return (
    <AbsoluteFill>
      <Backdrop glow="rgba(155,123,255,0.16)" />

      <div style={{position: 'absolute', left: 150, top: 96, width: 1600}}>
        <Kicker color={C.violet}>Safe by construction</Kicker>
        <div style={{height: 18}} />
        <Headline
          text="The LLM only proposes. Four gates decide."
          delay={4}
          size={70}
          highlight={{LLM: C.violet, Four: C.accent}}
        />
      </div>

      {/* Conveyor line */}
      <div
        style={{
          position: 'absolute',
          left: 120,
          top: Y,
          width: interpolate(frame, [6, 40], [0, 1680], clamp),
          height: 2,
          background: `linear-gradient(90deg, transparent, ${C.line} 10%, ${C.line} 90%, transparent)`,
        }}
      />

      {GATES.map((g, i) => {
        const appear = sp(frame, fps, 10 + i * 5, BOUNCY);
        const passed = frame >= T[i + 1];
        const hit = interpolate(frame, [T[i + 1] - 4, T[i + 1], T[i + 1] + 16], [0, 1, 0], clamp);
        const col = passed ? (g.warn ? C.warn : C.accent) : C.faint;
        return (
          <React.Fragment key={g.name}>
            <div
              style={{
                position: 'absolute',
                left: g.x - 6,
                top: Y - 150,
                width: 12,
                height: 300,
                borderRadius: 8,
                background: passed
                  ? `linear-gradient(${col}, rgba(0,0,0,0))`
                  : `linear-gradient(rgba(242,243,239,0.25), rgba(0,0,0,0))`,
                boxShadow: passed ? `0 0 ${20 + hit * 60}px ${col}` : 'none',
                transform: `scaleY(${appear})`,
                transformOrigin: 'top',
              }}
            />
            <div
              style={{
                position: 'absolute',
                left: g.x - 40,
                top: Y - 150,
                width: 80,
                height: 300,
                background: `linear-gradient(90deg, transparent, ${col}, transparent)`,
                opacity: hit * 0.35,
              }}
            />
            <div
              style={{
                position: 'absolute',
                left: g.x - 140,
                top: Y + 180,
                width: 280,
                textAlign: 'center',
                opacity: appear,
              }}
            >
              <div
                style={{
                  display: 'inline-flex',
                  width: 46,
                  height: 46,
                  borderRadius: '50%',
                  alignItems: 'center',
                  justifyContent: 'center',
                  border: `2px solid ${col}`,
                  color: passed ? C.ink : C.faint,
                  background: passed ? col : 'transparent',
                  fontFamily: FONT.sans,
                  fontWeight: 800,
                  fontSize: 22,
                  transform: `scale(${passed ? sp(frame, fps, T[i + 1], BOUNCY) * 0.25 + 0.75 + 0.25 * hit : 0.75})`,
                }}
              >
                {passed ? (g.warn ? '!' : '✓') : i + 1}
              </div>
              <div style={{fontFamily: FONT.sans, fontSize: 28, fontWeight: 700, color: C.ivory, marginTop: 12}}>
                {g.name}
              </div>
              <div
                style={{
                  fontFamily: FONT.mono,
                  fontSize: 19,
                  color: g.warn && passed ? C.warn : C.muted,
                  marginTop: 6,
                  opacity: passed ? 1 : 0.5,
                }}
              >
                {g.detail}
              </div>
            </div>
          </React.Fragment>
        );
      })}

      {/* The proposal */}
      <Glass
        style={{
          position: 'absolute',
          left: cardX - 170,
          top: Y - 90,
          width: 340,
          padding: '20px 24px',
          transform: `scale(${cardIn})`,
          border: `2px solid ${cardColor}`,
          boxShadow: `0 0 40px ${cardColor}55, 0 30px 80px rgba(0,0,0,0.5)`,
          background: 'rgba(10,14,30,0.9)',
        }}
      >
        <div style={{fontFamily: FONT.mono, fontSize: 17, letterSpacing: 3, color: cardColor}}>
          {approved ? 'APPROVED · EXECUTING' : flagged ? 'AWAITING HUMAN' : 'LLM · PROPOSAL'}
        </div>
        <div style={{fontFamily: FONT.mono, fontSize: 23, color: C.ivory, marginTop: 10, lineHeight: 1.4}}>
          rollback_config(
          <br />
          &nbsp;&nbsp;<span style={{color: C.gold}}>payments</span>, v2 → v1)
        </div>
      </Glass>

      {/* A human approves the risky step */}
      {frame >= CALL && (
        <Glass
          style={{
            position: 'absolute',
            left: 310,
            top: 905,
            width: 1300,
            padding: '22px 34px',
            display: 'flex',
            alignItems: 'center',
            gap: 30,
            ...rise(frame, fps, CALL, 40),
          }}
          tint={approved ? 'rgba(46,230,166,0.08)' : 'rgba(255,176,32,0.07)'}
        >
          <div style={{fontFamily: FONT.mono, fontSize: 19, letterSpacing: 3, color: approved ? C.accent : C.warn, width: 300}}>
            {approved ? 'APPROVED BY ON-CALL' : 'HIGH RISK · ASKING A HUMAN'}
          </div>
          <div style={{flex: 1, fontFamily: FONT.sans}}>
            <div style={{fontSize: 30, color: C.ivory}}>Roll back payments to v1?</div>
            <div style={{fontSize: 21, color: C.muted, marginTop: 4}}>action, risk, blast radius and evidence on one card</div>
          </div>
          <div
            style={{
              fontFamily: FONT.sans,
              fontSize: 28,
              fontWeight: 700,
              padding: '14px 34px',
              borderRadius: 999,
              border: `2px solid ${C.accent}`,
              background: approved ? C.accent : 'transparent',
              color: approved ? C.ink : C.accent,
              boxShadow: approved ? '0 0 40px rgba(46,230,166,0.55)' : 'none',
              transform: `scale(${0.88 + 0.12 * sp(frame, fps, OK, BOUNCY)})`,
            }}
          >
            {approved ? 'Approved' : 'Approve'}
          </div>
        </Glass>
      )}
    </AbsoluteFill>
  );
};
