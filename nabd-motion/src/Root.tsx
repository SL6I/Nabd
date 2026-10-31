import React from 'react';
import {Composition} from 'remotion';
import {NabdLogo} from './Logo';
import {NabdPromo, NabdPromoShort, SHORT_FRAMES, TOTAL_FRAMES} from './Video';
import {FPS, H, W} from './theme';

export const RemotionRoot: React.FC = () => (
  <>
    <Composition id="NabdPromo" component={NabdPromo} durationInFrames={TOTAL_FRAMES} fps={FPS} width={W} height={H} />
    <Composition id="NabdPromoShort" component={NabdPromoShort} durationInFrames={SHORT_FRAMES} fps={FPS} width={W} height={H} />
    <Composition id="NabdLogo" component={NabdLogo} durationInFrames={1} fps={FPS} width={400} height={400} />
  </>
);
