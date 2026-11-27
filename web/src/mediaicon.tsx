import type { ReactNode } from "react";
import type { MediaChoice } from "./selection";
import "./mediaicon.css";

// Drawn on a 16 by 16 grid in the colour of the text around them.
const DRAWINGS: Record<MediaChoice | "none", ReactNode> = {
  any: (
    <>
      <rect x="2" y="2" width="5" height="5" rx="1" />
      <rect x="9" y="2" width="5" height="5" rx="1" />
      <rect x="2" y="9" width="5" height="5" rx="1" />
      <rect x="9" y="9" width="5" height="5" rx="1" />
    </>
  ),
  web: (
    <>
      <circle cx="8" cy="8" r="6" />
      <ellipse cx="8" cy="8" rx="2.6" ry="6" />
      <path d="M2 8h12" />
    </>
  ),
  pdf: (
    <>
      <path d="M3.5 1.5h6l3 3v10h-9z" />
      <path d="M9.5 1.5v3h3M5.5 8h5M5.5 10.5h5" />
    </>
  ),
  recording: <path d="M2.5 6.5v3M5 4v8M7.5 2v12M10 5v6M12.5 6.5v3" />,
  none: <rect x="3" y="3" width="10" height="10" rx="2" strokeDasharray="2 2" />,
};

export const NO_MEDIA_NAME = "类型不明";

// The icon of a media. Where it stands beside the media's name it is not heard; where it stands
// alone, as before an item's title, it is heard by the name it is given.
export function MediaIcon({ media, heardAs }: { media: MediaChoice | "none"; heardAs?: string }) {
  return (
    <svg
      className="media-icon"
      viewBox="0 0 16 16"
      aria-hidden={heardAs === undefined}
      role={heardAs === undefined ? undefined : "img"}
      aria-label={heardAs}
    >
      {DRAWINGS[media]}
    </svg>
  );
}
