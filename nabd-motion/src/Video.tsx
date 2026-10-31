import React from 'react';
import {AbsoluteFill, useCurrentFrame, useVideoConfig} from 'remotion';
import {
  linearTiming,
  springTiming,
  TransitionPresentation,
  TransitionTiming,
  TransitionSeries,
} from '@remotion/transitions';
import {fade} from '@remotion/transitions/fade';
import {slide} from '@remotion/transitions/slide';
import {wipe} from '@remotion/transitions/wipe';
import {C} from './theme';
import {IntroScene} from './scenes/Intro';
import {ProblemScene} from './scenes/Problem';
import {DukkanScene, STORM} from './scenes/Dukkan';
import {LoopScene} from './scenes/Loop';
import {MizanScene} from './scenes/Mizan';
import {LearnsScene} from './scenes/Learns';
import {SafetyScene} from './scenes/Safety';
import {BenchmarkScene} from './scenes/Benchmark';
import {OutroScene} from './scenes/Outro';

type Scene = {C: React.FC; d: number};

const FULL: Scene[] = [
  {C: IntroScene, d: 180},
  {C: ProblemScene, d: 250},
  {C: DukkanScene, d: 430},
  {C: LoopScene, d: 360},
  {C: MizanScene, d: 370},
  {C: LearnsScene, d: 390},
  {C: SafetyScene, d: 310},
  {C: BenchmarkScene, d: 290},
  {C: OutroScene, d: 220},
];

// The ~70-second cut for a 3-minute submission. Each scene lasts as long as its voice-over line, so the
// narration and the picture change together. Dukkan ends before its perfect storm, so only the store and
// its map are shown; Safety runs past its "approved" beat (frame 252). The live demo shows the loop for
// real, so that scene is left out.
const SHORT: Scene[] = [
  {C: IntroScene, d: 201},
  {C: DukkanScene, d: STORM - 16},
  {C: ProblemScene, d: 308},
  {C: MizanScene, d: 378},
  {C: LearnsScene, d: 328},
  {C: SafetyScene, d: 290},
  {C: BenchmarkScene, d: 363},
  {C: OutroScene, d: 200},
];

const TR = 18;
// eslint-disable-next-line @typescript-eslint/no-explicit-any
const TRANSITIONS: {p: TransitionPresentation<any>; t: TransitionTiming}[] = [
  {p: fade(), t: linearTiming({durationInFrames: TR})},
  {p: slide({direction: 'from-right'}), t: springTiming({config: {damping: 200}, durationInFrames: TR})},
  {p: wipe({direction: 'from-left'}), t: linearTiming({durationInFrames: TR})},
  {p: slide({direction: 'from-bottom'}), t: springTiming({config: {damping: 200}, durationInFrames: TR})},
  {p: fade(), t: linearTiming({durationInFrames: TR})},
  {p: wipe({direction: 'from-right'}), t: linearTiming({durationInFrames: TR})},
  {p: slide({direction: 'from-right'}), t: springTiming({config: {damping: 200}, durationInFrames: TR})},
  {p: fade(), t: linearTiming({durationInFrames: TR})},
];

const frames = (scenes: Scene[]) => scenes.reduce((a, s) => a + s.d, 0) - TR * (scenes.length - 1);
export const TOTAL_FRAMES = frames(FULL);
export const SHORT_FRAMES = frames(SHORT);

// A thin line along the bottom edge that fills as the promo plays.
const Progress: React.FC = () => {
  const frame = useCurrentFrame();
  const {durationInFrames} = useVideoConfig();
  return (
    <AbsoluteFill style={{justifyContent: 'flex-end'}}>
      <div style={{height: 6, background: C.line}}>
        <div style={{height: 6, width: `${(frame / (durationInFrames - 1)) * 100}%`, background: C.accent}} />
      </div>
    </AbsoluteFill>
  );
};

const Promo: React.FC<{scenes: Scene[]; progress?: boolean}> = ({scenes, progress}) => (
  <AbsoluteFill style={{background: C.ink}}>
    <TransitionSeries>
      {scenes.map((s, i) => (
        <React.Fragment key={i}>
          <TransitionSeries.Sequence durationInFrames={s.d}>
            <s.C />
          </TransitionSeries.Sequence>
          {i < scenes.length - 1 && (
            <TransitionSeries.Transition presentation={TRANSITIONS[i].p} timing={TRANSITIONS[i].t} />
          )}
        </React.Fragment>
      ))}
    </TransitionSeries>
    {progress && <Progress />}
  </AbsoluteFill>
);

export const NabdPromo: React.FC = () => <Promo scenes={FULL} />;
export const NabdPromoShort: React.FC = () => <Promo scenes={SHORT} progress />;
