// What a list of the list column says while reading it again fails: why the service refused it,
// in the service's own words, or that the service does not answer and is being tried again.
export function Interruption({ refusal }: { refusal: string | undefined }) {
  return <p className="status failed">{refusal ?? "连接不上服务，正在重试…"}</p>;
}
