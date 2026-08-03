"use strict";

(function exposeMarkdownRenderer(global) {
  const headingPattern = /^(#{1,6})\s+(.+)$/;
  const unorderedPattern = /^(\s*)[-+*]\s+(.+)$/;
  const orderedPattern = /^(\s*)\d+[.)]\s+(.+)$/;
  const quotePattern = /^\s*>\s?(.*)$/;
  const fencePattern = /^\s*```\s*([^\s]*)\s*$/;
  const rulePattern = /^\s{0,3}((\*\s*){3,}|(-\s*){3,}|(_\s*){3,})$/;

  function appendInline(parent, source) {
    const text = String(source);
    let cursor = 0;
    let plainStart = 0;

    const flushPlain = (end) => {
      if (end > plainStart) {
        parent.append(document.createTextNode(text.slice(plainStart, end)));
      }
    };

    while (cursor < text.length) {
      let markerLength = 0;
      if (text.startsWith("**", cursor) || text.startsWith("__", cursor)) {
        markerLength = 2;
      }
      if (markerLength) {
        const marker = text.slice(cursor, cursor + markerLength);
        const end = text.indexOf(marker, cursor + markerLength);
        if (end > cursor + markerLength) {
          flushPlain(cursor);
          const strong = document.createElement("strong");
          appendInline(strong, text.slice(cursor + markerLength, end));
          parent.append(strong);
          cursor = end + markerLength;
          plainStart = cursor;
          continue;
        }
      }

      if (text[cursor] === "`") {
        const end = text.indexOf("`", cursor + 1);
        if (end > cursor + 1) {
          flushPlain(cursor);
          const code = document.createElement("code");
          code.textContent = text.slice(cursor + 1, end);
          parent.append(code);
          cursor = end + 1;
          plainStart = cursor;
          continue;
        }
      }

      if (text[cursor] === "[") {
        const labelEnd = text.indexOf("](", cursor + 1);
        const targetEnd = labelEnd < 0 ? -1 : text.indexOf(")", labelEnd + 2);
        if (labelEnd > cursor + 1 && targetEnd > labelEnd + 2) {
          const rawTarget = text.slice(labelEnd + 2, targetEnd);
          const safeTarget = safeMarkdownUrl(rawTarget);
          if (safeTarget) {
            flushPlain(cursor);
            const link = document.createElement("a");
            appendInline(link, text.slice(cursor + 1, labelEnd));
            link.href = safeTarget;
            link.rel = "noopener noreferrer";
            if (new URL(safeTarget).origin !== window.location.origin) {
              link.target = "_blank";
            }
            parent.append(link);
            cursor = targetEnd + 1;
            plainStart = cursor;
            continue;
          }
        }
      }

      if (text[cursor] === "*" && text[cursor + 1] !== "*") {
        const end = text.indexOf("*", cursor + 1);
        if (end > cursor + 1) {
          flushPlain(cursor);
          const emphasis = document.createElement("em");
          appendInline(emphasis, text.slice(cursor + 1, end));
          parent.append(emphasis);
          cursor = end + 1;
          plainStart = cursor;
          continue;
        }
      }

      cursor += 1;
    }
    flushPlain(text.length);
  }

  function safeMarkdownUrl(rawTarget) {
    try {
      const url = new URL(rawTarget.trim(), window.location.href);
      if (url.protocol === "http:" || url.protocol === "https:") {
        return url.href;
      }
    } catch {
      // Invalid and unsafe URLs remain visible as plain Markdown text.
    }
    return null;
  }

  function indentationWidth(value) {
    return Array.from(value).reduce(
      (width, character) => width + (character === "\t" ? 2 : 1),
      0,
    );
  }

  function matchListItem(line) {
    const unordered = line.match(unorderedPattern);
    if (unordered) {
      return {
        ordered: false,
        indent: indentationWidth(unordered[1]),
        content: unordered[2],
      };
    }
    const ordered = line.match(orderedPattern);
    if (ordered) {
      return {
        ordered: true,
        indent: indentationWidth(ordered[1]),
        content: ordered[2],
      };
    }
    return null;
  }

  function splitTableRow(line) {
    let value = line.trim();
    if (value.startsWith("|")) value = value.slice(1);
    if (value.endsWith("|")) value = value.slice(0, -1);
    return value.split("|").map((cell) => cell.trim());
  }

  function isTableSeparator(line) {
    const cells = splitTableRow(line);
    return (
      cells.length > 0 &&
      cells.every((cell) => /^:?-{3,}:?$/.test(cell.replaceAll(" ", "")))
    );
  }

  function startsBlock(lines, index) {
    const line = lines[index] || "";
    return (
      line.trim() === "" ||
      fencePattern.test(line) ||
      headingPattern.test(line) ||
      matchListItem(line) !== null ||
      quotePattern.test(line) ||
      rulePattern.test(line) ||
      (line.includes("|") && isTableSeparator(lines[index + 1] || ""))
    );
  }

  function appendTable(fragment, lines, start) {
    const table = document.createElement("table");
    const head = document.createElement("thead");
    const headRow = document.createElement("tr");
    for (const cell of splitTableRow(lines[start])) {
      const heading = document.createElement("th");
      appendInline(heading, cell);
      headRow.append(heading);
    }
    head.append(headRow);
    table.append(head);

    const body = document.createElement("tbody");
    let index = start + 2;
    while (index < lines.length && lines[index].trim() && lines[index].includes("|")) {
      const row = document.createElement("tr");
      for (const cell of splitTableRow(lines[index])) {
        const data = document.createElement("td");
        appendInline(data, cell);
        row.append(data);
      }
      body.append(row);
      index += 1;
    }
    if (body.childElementCount) table.append(body);
    fragment.append(table);
    return index;
  }

  function appendList(parent, lines, start, baseIndent) {
    const first = matchListItem(lines[start]);
    const list = document.createElement(first.ordered ? "ol" : "ul");
    let index = start;
    let lastItem = null;

    while (index < lines.length) {
      const current = matchListItem(lines[index]);
      if (!current || current.indent < baseIndent) break;
      if (current.indent > baseIndent) {
        if (!lastItem) break;
        index = appendList(lastItem, lines, index, current.indent);
        continue;
      }
      if (current.ordered !== first.ordered) break;

      lastItem = document.createElement("li");
      appendInline(lastItem, current.content);
      list.append(lastItem);
      index += 1;
    }

    parent.append(list);
    return index;
  }

  function renderMarkdown(container, markdown) {
    const lines = String(markdown).replace(/\r\n?/g, "\n").split("\n");
    const fragment = document.createDocumentFragment();
    let index = 0;

    while (index < lines.length) {
      const line = lines[index];
      if (!line.trim()) {
        index += 1;
        continue;
      }

      const fence = line.match(fencePattern);
      if (fence) {
        const codeLines = [];
        index += 1;
        while (index < lines.length && !fencePattern.test(lines[index])) {
          codeLines.push(lines[index]);
          index += 1;
        }
        if (index < lines.length) index += 1;
        const pre = document.createElement("pre");
        const code = document.createElement("code");
        code.textContent = codeLines.join("\n");
        if (fence[1]) code.dataset.language = fence[1];
        pre.append(code);
        fragment.append(pre);
        continue;
      }

      const heading = line.match(headingPattern);
      if (heading) {
        const element = document.createElement(`h${heading[1].length}`);
        appendInline(element, heading[2]);
        fragment.append(element);
        index += 1;
        continue;
      }

      if (rulePattern.test(line)) {
        fragment.append(document.createElement("hr"));
        index += 1;
        continue;
      }

      if (line.includes("|") && isTableSeparator(lines[index + 1] || "")) {
        index = appendTable(fragment, lines, index);
        continue;
      }

      const quote = line.match(quotePattern);
      if (quote) {
        const blockquote = document.createElement("blockquote");
        const quoteLines = [];
        while (index < lines.length) {
          const current = lines[index].match(quotePattern);
          if (!current) break;
          quoteLines.push(current[1]);
          index += 1;
        }
        appendInline(blockquote, quoteLines.join("\n"));
        fragment.append(blockquote);
        continue;
      }

      const listItem = matchListItem(line);
      if (listItem) {
        index = appendList(fragment, lines, index, listItem.indent);
        continue;
      }

      const paragraphLines = [line.trim()];
      index += 1;
      while (index < lines.length && !startsBlock(lines, index)) {
        paragraphLines.push(lines[index].trim());
        index += 1;
      }
      const paragraph = document.createElement("p");
      appendInline(paragraph, paragraphLines.join(" "));
      fragment.append(paragraph);
    }

    container.replaceChildren(fragment);
  }

  global.FileAgentMarkdown = Object.freeze({ renderMarkdown });
})(window);
