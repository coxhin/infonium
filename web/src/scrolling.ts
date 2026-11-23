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

// What the element's scroller shows, in the coordinates of the window.
export function viewOf(element: Element): DOMRect {
  return (
    scrollerOf(element)?.getBoundingClientRect() ?? new DOMRect(0, 0, innerWidth, innerHeight)
  );
}

export function inView(element: Element): boolean {
  const view = viewOf(element);
  const place = element.getBoundingClientRect();
  return place.bottom > view.top && place.top < view.bottom;
}
