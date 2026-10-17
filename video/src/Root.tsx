import { Composition, staticFile } from "remotion";
import { Demo, FPS, GAP, type Clips, type Timing } from "./Demo";

export const Root = () => (
  <Composition
    id="Demo"
    component={Demo}
    width={1920}
    height={1080}
    fps={FPS}
    durationInFrames={1}
    defaultProps={{ timing: [] as Timing[], clips: {} as Clips }}
    calculateMetadata={async () => {
      // scene lengths come from the voice-over; the screen segments from the recording
      const timing: Timing[] = await (await fetch(staticFile("timing.json"))).json();
      const clips: Clips = await (await fetch(staticFile("clips.json"))).json();
      const frames = timing.reduce((n, t) => n + Math.ceil(t.duration * FPS) + GAP, 0);
      return { durationInFrames: frames, props: { timing, clips } };
    }}
  />
);
