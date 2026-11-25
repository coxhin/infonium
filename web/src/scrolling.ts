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

// The line, in the coordinates of the window, below which the element's scroller shows it
// uncovered: the top of what the scroller shows, or the bottom of a sticky element before the
// element that covers that top, as the head of the reader does.
export function uncoveredTop(element: Element): number {
  const scroller = scrollerOf(element);
  let top = viewOf(element).top;
  for (let inner: Element | null = element; inner !== null && inner !== scroller; ) {
    for (let before = inner.previousElementSibling; before !== null; ) {
      const place = before.getBoundingClientRect();
      if (
        getComputedStyle(before).position === "sticky" &&
        place.top <= top + 1 &&
        place.bottom > top
      ) {
        top = place.bottom;
      }
      before = before.previousElementSibling;
    }
    inner = inner.parentElement;
  }
  return top;
}

export function inView(element: Element): boolean {
  const view = viewOf(element);
  const place = element.getBoundingClientRect();
  return place.bottom > view.top && place.top < view.bottom;
}
