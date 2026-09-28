import React from 'react';
import {
  AbsoluteFill,
  Img,
  OffthreadVideo,
  Sequence,
  interpolate,
  spring,
  staticFile,
  useCurrentFrame,
  useVideoConfig,
} from 'remotion';

type Zone = 'left' | 'right' | 'top' | 'bottom' | 'center';

type SubjectTrack = {
  time: number;
  face: {x: number; y: number; width: number; height: number};
  free_zone: Zone;
  detected: boolean;
};

type SemanticEvent = {
  event_id: string;
  segment_id?: string | number;
  start: number;
  end: number;
  anchor: number;
  intent:
    | 'warning'
    | 'metric'
    | 'question'
    | 'contrast'
    | 'process'
    | 'sequence'
    | 'reveal'
    | 'explanation'
    | 'statement';
  label: string;
  strength: number;
  reason: string;
  effect: string;
};

type VisualEvent = {
  event_id: string;
  start: number;
  end: number;
  type: string;
  title: string;
  treatment: 'cutaway' | 'overlay_card' | string;
  animation: string;
  asset_src: string;
  placement_zone?: Zone;
};

export type HermesMotionProps = {
  schema_version: number;
  renderer: string;
  composition_id: string;
  width: number;
  height: number;
  fps: number;
  duration: number;
  duration_in_frames: number;
  video_src: string;
  style: {
    preset: string;
    accent: string;
    density: string;
    placement: string;
    motion_language: string;
  };
  subject_tracking: {
    source: string;
    sample_seconds: number;
    tracks: SubjectTrack[];
  };
  semantic_events: SemanticEvent[];
  visual_events: VisualEvent[];
  qa: {
    opening_reserved_until: number;
    transcript_status: string;
    transcript_warnings: unknown[];
    high_risk_word_count: number;
    do_not_invent_claims: boolean;
    duplicate_text_overlay: boolean;
  };
};

const clamp = (value: number, minimum = 0, maximum = 1) =>
  Math.min(maximum, Math.max(minimum, value));

const hexToRgba = (hex: string, alpha: number) => {
  const cleaned = hex.replace('#', '');
  const normalized = cleaned.length === 6 ? cleaned : '47D7FF';
  const red = Number.parseInt(normalized.slice(0, 2), 16);
  const green = Number.parseInt(normalized.slice(2, 4), 16);
  const blue = Number.parseInt(normalized.slice(4, 6), 16);
  return `rgba(${red}, ${green}, ${blue}, ${alpha})`;
};

const envelope = (
  time: number,
  start: number,
  end: number,
  edgeSeconds = 0.22,
) => {
  if (time < start || time > end) return 0;
  const fadeIn = clamp((time - start) / edgeSeconds);
  const fadeOut = clamp((end - time) / edgeSeconds);
  return Math.min(fadeIn, fadeOut);
};

const trackAt = (tracks: SubjectTrack[], time: number): SubjectTrack => {
  if (!tracks.length) {
    return {
      time: 0,
      face: {x: 0.32, y: 0.16, width: 0.36, height: 0.38},
      free_zone: 'bottom',
      detected: false,
    };
  }
  let selected = tracks[0];
  for (const track of tracks) {
    if (track.time > time) break;
    selected = track;
  }
  return selected;
};

const placement = (zone: Zone): React.CSSProperties => {
  const shared: React.CSSProperties = {
    position: 'absolute',
    width: 270,
    maxWidth: '44%',
  };
  if (zone === 'left') return {...shared, left: 58, top: 370};
  if (zone === 'right') return {...shared, right: 58, top: 370};
  if (zone === 'top') return {...shared, left: 225, top: 150};
  if (zone === 'bottom') return {...shared, left: 225, bottom: 245};
  return {...shared, left: 225, top: 420};
};

const IntentGlyph: React.FC<{
  intent: SemanticEvent['intent'];
  progress: number;
  accent: string;
}> = ({intent, progress, accent}) => {
  if (intent === 'question') {
    return (
      <div
        style={{
          width: 48,
          height: 48,
          borderRadius: 24,
          border: `2px solid ${accent}`,
          color: accent,
          display: 'grid',
          placeItems: 'center',
          fontSize: 28,
          fontWeight: 800,
          transform: `rotate(${interpolate(progress, [0, 1], [-18, 0])}deg)`,
        }}
      >
        ?
      </div>
    );
  }
  if (intent === 'process' || intent === 'sequence') {
    return (
      <div style={{display: 'flex', alignItems: 'center', gap: 7}}>
        {[0, 1, 2].map((index) => (
          <React.Fragment key={index}>
            <div
              style={{
                width: index === 1 ? 15 : 11,
                height: index === 1 ? 15 : 11,
                borderRadius: 20,
                background: progress > index * 0.2 ? accent : 'rgba(255,255,255,.22)',
                boxShadow:
                  progress > index * 0.2
                    ? `0 0 18px ${hexToRgba(accent, 0.55)}`
                    : 'none',
              }}
            />
            {index < 2 ? (
              <div
                style={{
                  width: 23,
                  height: 2,
                  background: hexToRgba(accent, 0.5),
                }}
              />
            ) : null}
          </React.Fragment>
        ))}
      </div>
    );
  }
  if (intent === 'contrast') {
    return (
      <div style={{display: 'flex', alignItems: 'center', gap: 9}}>
        <div style={{width: 21, height: 21, border: `2px solid ${accent}`}} />
        <div style={{width: 2, height: 33, background: accent}} />
        <div style={{width: 21, height: 21, background: accent}} />
      </div>
    );
  }
  return (
    <div
      style={{
        width: 50,
        height: 6,
        borderRadius: 8,
        background: accent,
        boxShadow: `0 0 22px ${hexToRgba(accent, 0.6)}`,
        transform: `scaleX(${0.35 + progress * 0.65})`,
        transformOrigin: 'left center',
      }}
    />
  );
};

const SemanticCue: React.FC<{
  event: SemanticEvent;
  accent: string;
  zone: Zone;
}> = ({event, accent, zone}) => {
  const frame = useCurrentFrame();
  const {fps, durationInFrames} = useVideoConfig();
  const enter = spring({
    frame,
    fps,
    config: {damping: 18, stiffness: 150, mass: 0.75},
    durationInFrames: Math.min(18, durationInFrames),
  });
  const exit = interpolate(
    frame,
    [Math.max(0, durationInFrames - 10), durationInFrames],
    [1, 0],
    {extrapolateLeft: 'clamp', extrapolateRight: 'clamp'},
  );
  const direction = zone === 'left' ? -1 : zone === 'right' ? 1 : 0;
  const opacity = enter * exit;
  const emphasis = event.intent === 'warning' ? '#FF725E' : accent;
  return (
    <div
      style={{
        ...placement(zone),
        opacity,
        transform: `translate3d(${direction * (1 - enter) * 34}px, ${(1 - enter) * 18}px, 0) scale(${0.96 + enter * 0.04})`,
        transformOrigin: zone === 'right' ? 'right center' : 'left center',
        padding: '18px 18px 17px',
        borderRadius: 24,
        border: `1px solid ${hexToRgba(emphasis, 0.42)}`,
        background: 'linear-gradient(145deg, rgba(8,12,20,.82), rgba(14,19,30,.68))',
        boxShadow: `0 18px 50px rgba(0,0,0,.34), 0 0 34px ${hexToRgba(emphasis, 0.12)}`,
        backdropFilter: 'blur(12px)',
        color: '#F8FAFC',
        fontFamily: 'Inter, Segoe UI, sans-serif',
      }}
    >
      <IntentGlyph intent={event.intent} progress={enter} accent={emphasis} />
    </div>
  );
};

const VisualExplainer: React.FC<{event: VisualEvent}> = ({event}) => {
  const frame = useCurrentFrame();
  const {fps, durationInFrames} = useVideoConfig();
  const enter = spring({
    frame,
    fps,
    config: {damping: 20, stiffness: 115, mass: 0.9},
    durationInFrames: Math.min(22, durationInFrames),
  });
  const exit = interpolate(
    frame,
    [Math.max(0, durationInFrames - 12), durationInFrames],
    [1, 0],
    {extrapolateLeft: 'clamp', extrapolateRight: 'clamp'},
  );
  const isCutaway = event.treatment === 'cutaway';
  const zone = event.placement_zone || 'center';
  const containedPlacement: React.CSSProperties = isCutaway
    ? {inset: 0}
    : zone === 'left'
      ? {left: -24, top: '12.5%', width: '75%', height: '75%'}
      : zone === 'right'
        ? {right: -24, top: '12.5%', width: '75%', height: '75%'}
        : zone === 'top'
          ? {left: '10%', top: -70, width: '80%', height: '80%'}
          : {left: '10%', bottom: -70, width: '80%', height: '80%'};
  return (
    <div
      style={{
        position: 'absolute',
        ...containedPlacement,
        opacity: enter * exit,
        transform: `translate3d(${(1 - enter) * (isCutaway ? 0 : 38)}px, 0, 0) scale(${isCutaway ? 1.015 - enter * 0.015 : 0.96 + enter * 0.04})`,
      }}
    >
      <Img
        src={staticFile(event.asset_src)}
        style={{width: '100%', height: '100%', objectFit: 'contain'}}
      />
    </div>
  );
};

export const HermesSemanticMotion: React.FC<HermesMotionProps> = (props) => {
  const frame = useCurrentFrame();
  const {fps} = useVideoConfig();
  const time = frame / fps;
  const activeVisual = props.visual_events.some(
    (event) => time >= event.start && time <= event.end,
  );
  const activeSemantic = props.semantic_events.filter(
    (event) => time >= event.start && time <= event.end,
  );
  const strongest = activeSemantic.sort((a, b) => b.strength - a.strength)[0];
  const subject = trackAt(props.subject_tracking.tracks, time);
  const pulse = strongest
    ? envelope(time, strongest.start, strongest.end) * strongest.strength
    : 0;
  const zoom = 1 + pulse * 0.038;
  const faceCenterX = (subject.face.x + subject.face.width / 2) * 100;
  const faceCenterY = (subject.face.y + subject.face.height / 2) * 100;

  return (
    <AbsoluteFill style={{backgroundColor: '#05070B', overflow: 'hidden'}}>
      <AbsoluteFill
        style={{
          transform: `scale(${zoom})`,
          transformOrigin: `${faceCenterX}% ${faceCenterY}%`,
          filter: strongest?.intent === 'reveal' ? `saturate(${1 + pulse * 0.1})` : undefined,
        }}
      >
        {props.video_src ? (
          <OffthreadVideo
            src={staticFile(props.video_src)}
            style={{width: '100%', height: '100%', objectFit: 'cover'}}
          />
        ) : null}
      </AbsoluteFill>

      {props.visual_events.map((event) => {
        const from = Math.max(0, Math.round(event.start * fps));
        const duration = Math.max(1, Math.round((event.end - event.start) * fps));
        return (
          <Sequence key={event.event_id} from={from} durationInFrames={duration}>
            <VisualExplainer event={event} />
          </Sequence>
        );
      })}

      {!activeVisual
        ? props.semantic_events.map((event) => {
            const from = Math.max(0, Math.round(event.start * fps));
            const duration = Math.max(
              1,
              Math.round((event.end - event.start) * fps),
            );
            const zone = trackAt(props.subject_tracking.tracks, event.anchor).free_zone;
            return (
              <Sequence key={event.event_id} from={from} durationInFrames={duration}>
                <SemanticCue event={event} accent={props.style.accent} zone={zone} />
              </Sequence>
            );
          })
        : null}

      <AbsoluteFill
        style={{
          pointerEvents: 'none',
          background:
            'radial-gradient(circle at 50% 42%, transparent 50%, rgba(0,0,0,.22) 100%)',
        }}
      />
    </AbsoluteFill>
  );
};
