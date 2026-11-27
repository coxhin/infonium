// Names in the order a Chinese reader's phone gives contacts: those that begin with a digit
// first; then those under each letter from A to Z, a name in Latin letters under its first letter
// and a Chinese name under the first letter of its first character's pinyin (安 under A, 北 and 播
// under B, 田 under T); then those in any other script. Within one group, by the collator, which
// orders Chinese by its pinyin and the numbers in names by their value, so that 分类 9 comes
// before 分类 10.
const NAME_ORDER = new Intl.Collator("zh-Hans-CN", { numeric: true });

// The letters pinyin begins with: none begins with I, U or V.
const INITIALS = "ABCDEFGHJKLMNOPQRSTWXYZ";
// The first ideograph, in the collator's order, of those whose pinyin begins with each of the
// letters above, as the collation's own index has them: an ideograph is under the letter of the
// last of these that does not come after it.
const FIRST_OF_INITIAL = [..."吖丷嚓咑妸发旮哈丌咔垃呣拏喔妑七呥仨他屲夕丫帀"];

const DIGIT = /^\p{Nd}/u;
const IDEOGRAPH = /^\p{Script=Han}/u;
const LATIN_LETTER = /^[A-Z]$/;
const DIGITS_GROUP = 0;
const OTHER_GROUP = 27;

// The group a name stands in: the digits; each letter from A to Z, as 1 to 26; or any other
// script. Names are few and come again at every ordering, so each is grouped once.
const groups = new Map<string, number>();

function letterGroup(letter: string): number {
  return letter.charCodeAt(0) - "A".charCodeAt(0) + 1;
}

function groupOf(name: string): number {
  const known = groups.get(name);
  if (known !== undefined) {
    return known;
  }
  const first = String.fromCodePoint(name.codePointAt(0) ?? 0);
  // A letter with marks, or written full width, is under its plain letter.
  const plain = first.normalize("NFKD").charAt(0).toUpperCase();
  let initial = -1;
  if (IDEOGRAPH.test(first)) {
    FIRST_OF_INITIAL.forEach((ideograph, at) => {
      if (NAME_ORDER.compare(ideograph, first) <= 0) {
        initial = at;
      }
    });
  }
  const group = DIGIT.test(first)
    ? DIGITS_GROUP
    : LATIN_LETTER.test(plain)
      ? letterGroup(plain)
      : initial >= 0
        ? letterGroup(INITIALS.charAt(initial))
        : OTHER_GROUP;
  groups.set(name, group);
  return group;
}

export function byName(one: string, other: string): number {
  return groupOf(one) - groupOf(other) || NAME_ORDER.compare(one, other);
}
