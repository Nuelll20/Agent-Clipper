import React from 'react';
import {CalculateMetadataFunction, Composition} from 'remotion';
import {
  HermesMotionProps,
  HermesSemanticMotion,
} from './HermesSemanticMotion';

const defaultProps: HermesMotionProps = {
  schema_version: 1,
  renderer: 'motioncraft-remotion',
  composition_id: 'HermesSemanticMotion',
  width: 720,
  height: 1280,
  fps: 30,
  duration: 30,
  duration_in_frames: 900,
  video_src: '',
  style: {
    preset: 'pi-v2',
    accent: '#47D7FF',
    density: 'restrained',
    placement: 'subject-aware',
    motion_language: 'cinematic-minimal',
  },
  subject_tracking: {
    source: 'center_fallback',
    sample_seconds: 0.75,
    tracks: [
      {
        time: 0,
        face: {x: 0.32, y: 0.16, width: 0.36, height: 0.38},
        free_zone: 'bottom',
        detected: false,
      },
    ],
  },
  semantic_events: [],
  visual_events: [],
  qa: {
    opening_reserved_until: 5.2,
    transcript_status: 'passed',
    transcript_warnings: [],
    high_risk_word_count: 0,
    do_not_invent_claims: true,
    duplicate_text_overlay: false,
  },
};

const calculateMetadata: CalculateMetadataFunction<HermesMotionProps> = ({
  props,
}) => ({
  durationInFrames: Math.max(
    1,
    props.duration_in_frames || Math.ceil(props.duration * props.fps),
  ),
  fps: props.fps || 30,
  width: props.width || 720,
  height: props.height || 1280,
});

export const HermesRoot: React.FC = () => (
  <Composition
    id="HermesSemanticMotion"
    component={HermesSemanticMotion}
    durationInFrames={900}
    fps={30}
    width={720}
    height={1280}
    defaultProps={defaultProps}
    calculateMetadata={calculateMetadata}
  />
);
