import { useEffect, useRef, useState } from "react";
import type { TimePlace } from "./client";
import { reasonOf } from "./loading";

const PLAY_CUT_SHORT = "AbortError";
// Play starts this long before the moment pointed at: captions timed by hand may come a
// little after their words.
const LEAD_SECONDS = 1;

// Where the playing of a recording has come to, for the views that follow it. It is told the
// moment at every frame while the recording plays, so a view reads from it only what it
// needs, and is drawn again only when that changes.
export class Clock {
  moment = 0;
  started = false;
  private readonly listeners = new Set<() => void>();

  subscribe = (listener: () => void): (() => void) => {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  };

  tell(moment: number): void {
    this.moment = moment;
    this.started = true;
    this.listeners.forEach((listener) => listener());
  }
}

// Plays a recording, and starts from the first of the stretches that were pointed at. The
// stretches are given anew at every act of pointing, so pointing at one again plays it again.
// The clock is told where the playing has come to.
export function Player({
  source,
  pointed,
  clock,
}: {
  source: string;
  pointed: TimePlace[];
  clock: Clock;
}) {
  const audio = useRef<HTMLAudioElement>(null);
  const [failure, setFailure] = useState<string>();

  useEffect(() => {
    const first = pointed[0];
    if (first !== undefined && audio.current !== null) {
      audio.current.currentTime = Math.max(0, first.moment - LEAD_SECONDS);
      audio.current.play().then(
        () => setFailure(undefined),
        (error: unknown) => {
          // A play that a pause or another seek cuts short is no failure.
          if (!(error instanceof DOMException && error.name === PLAY_CUT_SHORT)) {
            setFailure(reasonOf(error));
          }
        },
      );
    }
  }, [pointed]);

  useEffect(() => {
    const played = audio.current;
    if (played === null) {
      return;
    }
    let frame: number | undefined;
    function tellFrame() {
      if (played !== null) {
        clock.tell(played.currentTime);
        frame = played.paused ? undefined : requestAnimationFrame(tellFrame);
      }
    }
    function tellOnce() {
      if (frame === undefined && played !== null) {
        tellFrame();
      }
    }
    // A moment reached by a seek while paused is told once; playing is followed frame by
    // frame until it pauses.
    played.addEventListener("play", tellOnce);
    played.addEventListener("seeked", tellOnce);
    played.addEventListener("timeupdate", tellOnce);
    return () => {
      played.removeEventListener("play", tellOnce);
      played.removeEventListener("seeked", tellOnce);
      played.removeEventListener("timeupdate", tellOnce);
      if (frame !== undefined) {
        cancelAnimationFrame(frame);
      }
    };
  }, [clock]);

  return (
    <div className="player">
      <audio ref={audio} controls preload="metadata" src={source} />
      {failure !== undefined && <p className="status failed">播放不了：{failure}</p>}
    </div>
  );
}
