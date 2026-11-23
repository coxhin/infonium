export function scrolls(overflow: string): boolean {
  return overflow === "auto" || overflow === "scroll";
}

// The nearest element around this one that scrolls its content, or none when the window does.
export function scrollerOf(element: Element): Element | null {
  for (
    let ancestor = element.parentElement;
    ancestor !== null;
    ancestor = ancestor.parentElement
  ) {
    if (scrolls(getComputedStyle(ancestor).overflowY)) {
      return ancestor;
    }
  }
  return null;
}
