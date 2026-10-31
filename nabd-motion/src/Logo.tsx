import React from 'react';
import {AbsoluteFill} from 'remotion';
import {Mark} from './ui';
import {C} from './theme';

// A square still of the brand mark with room around it, so it survives avatar slots that crop to a circle.
export const NabdLogo: React.FC = () => (
  <AbsoluteFill
    style={{
      justifyContent: 'center',
      alignItems: 'center',
      background: `radial-gradient(circle at 50% 50%, rgba(15, 107, 82, 0.55), ${C.bg} 70%)`,
    }}
  >
    <Mark size={220} />
  </AbsoluteFill>
);
