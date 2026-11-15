import { useEffect, useRef, useState } from "react";
import type { TimePlace } from "./client";
import { reasonOf } from "./loading";

// Plays a recording, and starts from the first of the stretches that were pointed at. The
// stretches are given anew at every act of pointing, so pointing at one again plays it again.
export function Player({ source, pointed }: { source: string; pointed: TimePlace[] }) {
  const audio = useRef<HTMLAudioElement>(null);
  const [failure, setFailure] = useState<string>();

  useEffect(() => {
    const first = pointed[0];
    if (first !== undefined && audio.current !== null) {
      audio.current.currentTime = first.moment;
      audio.current.play().then(
        () => setFailure(undefined),
        (error: unknown) => setFailure(reasonOf(error)),
      );
    }
  }, [pointed]);

  return (
    <div className="player">
      <audio ref={audio} controls preload="metadata" src={source} />
      {failure !== undefined && <p className="status failed">播放不了：{failure}</p>}
    </div>
  );
}
