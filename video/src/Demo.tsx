import React from "react";
import {
  AbsoluteFill, Audio, Freeze, OffthreadVideo, Series, interpolate, spring, staticFile, useCurrentFrame, useVideoConfig,
} from "remotion";
import { loadFont } from "@remotion/google-fonts/Archivo";

const { fontFamily } = loadFont("normal", { weights: ["500", "800", "900"], subsets: ["latin"] });

export const FPS = 30;
export const GAP = 8; // frames of breath between scenes
export type Word = { w: string; t0: number; t1: number };
export type Timing = { id: string; text: string; duration: number; words: Word[] };
export type Clips = Record<string, [number, number][]>;

// the app's own BLITZ palette
const C = { stage: "#D9D9E4", panel: "#F3F3F5", ink: "#000", soft: "#55555F", pink: "#D2246E", peach: "#FFC6A5", rose: "#F7A5C8", yellow: "#F1DC5B" };
const SCALE = 0.93;
const LAG = 0.8; // the recording runs about 0.8 s behind the recorder clock (measured on the Ask answer)
const W = Math.round(1440 * SCALE), H = Math.round(900 * SCALE);
const TOP = 34, LEFT = (1920 - W) / 2;

export const Demo: React.FC<{ timing: Timing[]; clips: Clips }> = ({ timing, clips }) => (
  <AbsoluteFill style={{ background: C.stage, fontFamily }}>
    <Series>
      {timing.map((t) => {
        const frames = Math.ceil(t.duration * FPS) + GAP;
        return (
          <Series.Sequence key={t.id} durationInFrames={frames}>
            <Audio src={staticFile(`${t.id}.mp3`)} />
            <Frame>
              {t.id === "intro" ? <TitleCard t={t} /> : t.id === "outro" ? <EndCard /> : <Screen segs={clips[t.id] || []} frames={frames} />}
            </Frame>
            <Subtitles words={t.words} />
          </Series.Sequence>
        );
      })}
    </Series>
  </AbsoluteFill>
);

const Frame: React.FC<{ children: React.ReactNode }> = ({ children }) => (
  <div style={{ position: "absolute", left: LEFT, top: TOP, width: W, height: H, border: `3px solid ${C.ink}`,
    borderRadius: 24, overflow: "hidden", background: C.panel, boxSizing: "content-box" }}>
    {children}
  </div>
);

// the recording, cut to the segments the recorder marked (long waits are left out)
const Screen: React.FC<{ segs: [number, number][]; frames: number }> = ({ segs, frames }) => {
  let left = frames;
  const parts: { a: number; n: number }[] = [];
  for (const [a, b] of segs) {
    const n = Math.min(left, Math.round((b - a) * FPS));
    if (n > 0) { parts.push({ a, n }); left -= n; }
  }
  const last = parts.at(-1);
  const src = staticFile("screen.webm");
  return (
    <Series>
      {parts.map((p, i) => (
        <Series.Sequence key={i} durationInFrames={p.n}>
          <OffthreadVideo src={src} muted trimBefore={Math.round((p.a + LAG) * FPS)} style={{ width: W, height: H }} />
        </Series.Sequence>
      ))}
      {last && left > 0 ? (
        // narration longer than the recorded scene: hold its last frame rather than run into the next scene
        <Series.Sequence durationInFrames={left}>
          <Freeze frame={last.n - 1}>
            <OffthreadVideo src={src} muted trimBefore={Math.round((last.a + LAG) * FPS)} style={{ width: W, height: H }} />
          </Freeze>
        </Series.Sequence>
      ) : null}
    </Series>
  );
};

// karaoke subtitles in the band under the frame: the line being spoken, the current word in the CTA gradient
const Subtitles: React.FC<{ words: Word[] }> = ({ words }) => {
  const t = useCurrentFrame() / FPS;
  const lines: Word[][] = [];
  let cur: Word[] = [];
  for (const w of words) {
    cur.push(w);
    const chars = cur.reduce((n, x) => n + x.w.length + 1, 0);
    if (chars > 52 || (/[.?!:]$/.test(w.w) && cur.length >= 3)) { lines.push(cur); cur = []; }
  }
  if (cur.length) lines.push(cur);
  const i = lines.findIndex((l, k) => t >= l[0].t0 - 0.15 && t < (lines[k + 1]?.[0].t0 ?? l.at(-1)!.t1 + 0.6) - 0.15);
  if (i < 0) return null;
  return (
    <div style={{ position: "absolute", left: 0, right: 0, top: TOP + H + 6, bottom: 0, display: "flex",
      alignItems: "center", justifyContent: "center" }}>
      <div style={{ fontSize: 44, fontWeight: 800, letterSpacing: "-0.01em", color: C.ink, textAlign: "center", lineHeight: 1.15 }}>
        {lines[i].map((w, k) => {
          const on = t >= w.t0 && t < w.t1 + 0.08;
          return (
            <span key={k} style={{ padding: "2px 8px", borderRadius: 999, margin: "0 1px",
              background: on ? `linear-gradient(90deg, ${C.peach}, ${C.rose})` : "transparent",
              opacity: t >= w.t0 - 0.05 ? 1 : 0.45 }}>{w.w}</span>
          );
        })}
      </div>
    </div>
  );
};

const at = (t: Timing, word: string) => t.words.find((w) => w.w.toLowerCase().startsWith(word))?.t0 ?? 0;

const Rise: React.FC<{ from: number; children: React.ReactNode; style?: React.CSSProperties }> = ({ from, children, style }) => {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();
  const s = spring({ frame: frame - Math.round(from * fps), fps, config: { damping: 200 } });
  return <div style={{ opacity: s, transform: `translateY(${interpolate(s, [0, 1], [40, 0])}px)`, ...style }}>{children}</div>;
};

const TitleCard: React.FC<{ t: Timing }> = ({ t }) => (
  <AbsoluteFill style={{ padding: "70px 80px", background: C.panel }}>
    <Glyphs />
    <Rise from={at(t, "december")}><div style={{ fontSize: 40, fontWeight: 800 }}>December 2022</div></Rise>
    <Rise from={at(t, "cancelled")}>
      <div style={{ fontSize: 132, fontWeight: 900, lineHeight: 0.92, letterSpacing: "-0.05em", marginTop: 18, maxWidth: 1050 }}>
        Nearly 17,000 flights cancelled.
      </div>
    </Rise>
    <Rise from={at(t, "fined")} style={{ marginTop: 34 }}>
      <span style={{ fontSize: 48, fontWeight: 800, padding: "10px 30px", borderRadius: 999,
        background: `linear-gradient(90deg, ${C.peach}, ${C.rose})` }}>$140 million fine</span>
    </Rise>
    <Rise from={at(t, "lost")} style={{ position: "absolute", left: 80, bottom: 70, maxWidth: 900 }}>
      <div style={{ fontSize: 44, fontWeight: 800, lineHeight: 1.1 }}>The airline lost track of how its operation connects.</div>
    </Rise>
  </AbsoluteFill>
);

const EndCard: React.FC = () => (
  <AbsoluteFill style={{ padding: "80px 80px", background: C.panel }}>
    <Glyphs />
    <Rise from={0}><div style={{ fontSize: 210, fontWeight: 900, lineHeight: 0.88, letterSpacing: "-0.055em" }}>IROPS Brain</div></Rise>
    <Rise from={0.6}><div style={{ fontSize: 56, fontWeight: 800, marginTop: 28 }}>The connections are the database.</div></Rise>
    <Rise from={1.2} style={{ position: "absolute", left: 80, bottom: 80, display: "flex", gap: 18, flexWrap: "wrap" }}>
      <Pill dark>irops-brain.vercel.app</Pill>
      <Pill>github.com/PugarHuda/irops-brain</Pill>
      <Pill>Built on FalkorDB</Pill>
    </Rise>
  </AbsoluteFill>
);

const Pill: React.FC<{ children: React.ReactNode; dark?: boolean }> = ({ children, dark }) => (
  <span style={{ fontSize: 34, fontWeight: 800, padding: "12px 28px", borderRadius: 999, border: `3px solid ${C.ink}`,
    background: dark ? C.ink : "white", color: dark ? C.panel : C.ink }}>{children}</span>
);

const Glyphs: React.FC = () => {
  const frame = useCurrentFrame();
  return (
    <>
      <svg viewBox="0 0 120 60" style={{ position: "absolute", width: 230, right: 120, top: 70, transform: "rotate(-6deg)" }}>
        <path d="M6 46 C 30 6, 70 4, 98 26" fill="none" stroke={C.yellow} strokeWidth={12} strokeLinecap="round"
          strokeDasharray={140} strokeDashoffset={interpolate(frame, [0, 24], [140, 0], { extrapolateRight: "clamp" })} />
        <path d="M84 8 L112 32 L78 40 Z" fill={C.yellow} opacity={interpolate(frame, [18, 26], [0, 1], { extrapolateLeft: "clamp", extrapolateRight: "clamp" })} />
      </svg>
      <div style={{ position: "absolute", right: 90, bottom: 60, fontSize: 120, lineHeight: 1,
        transform: `rotate(${frame * 0.6}deg)` }}>&#10033;</div>
    </>
  );
};
