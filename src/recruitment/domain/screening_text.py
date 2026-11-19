import re

GMAIL_ATTRIBUTION = re.compile(r"^On .+ wrote:$", re.IGNORECASE)
ORIGINAL_MESSAGE = re.compile(r"^-{2,}\s*Original Message\s*-{2,}$", re.IGNORECASE)
ACKNOWLEDGMENT = re.compile(
    r"(?:Thanks|Thank you|I understand|I appreciate)\b[\w ,’'\-]+[.!]", re.IGNORECASE
)
SCHEDULING_CLAIM = re.compile(
    r"\b(?:book\w*|confirm\w*|cancel\w*|reserv\w*|reschedul\w*|promise\w*|"
    r"guarantee\w*|held|hold\w*|call\w*|move\w*|hired|rejected|link\w*|https?\w*|www|"
    r"we|our|will|shall)\b",
    re.IGNORECASE,
)


def candidate_reply(body: str) -> str:
    lines: list[str] = []
    source = body.splitlines()
    index = 0
    while index < len(source):
        line = source[index].strip()
        if ORIGINAL_MESSAGE.fullmatch(line):
            break
        if line[:3].casefold() == "on ":
            attribution = line
            last = index
            while not GMAIL_ATTRIBUTION.fullmatch(attribution) and last < min(
                index + 4, len(source) - 1
            ):
                part = source[last + 1].strip()
                if not part or part.startswith(">"):
                    break
                attribution += f" {part}"
                last += 1
            if GMAIL_ATTRIBUTION.fullmatch(attribution) and (
                last == index
                or last + 1 == len(source)
                or source[last + 1].lstrip().startswith(">")
            ):
                index = last + 1
                continue
        if not line.startswith(">"):
            lines.append(source[index])
        index += 1
    return "\n".join(lines).strip()


def acknowledgment(value: str | None) -> str:
    text = (value or "").strip()
    if ACKNOWLEDGMENT.fullmatch(text) and not (
        any(character.isnumeric() for character in text) or SCHEDULING_CLAIM.search(text)
    ):
        return text
    return ""
