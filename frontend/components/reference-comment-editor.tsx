import { Editor, type JSONContent } from '@tiptap/core';
import { EditorContent } from '@tiptap/react';
import Document from '@tiptap/extension-document';
import HardBreak from '@tiptap/extension-hard-break';
import Paragraph from '@tiptap/extension-paragraph';
import Text from '@tiptap/extension-text';
import { UndoRedo } from '@tiptap/extensions';
import { useEffect, useLayoutEffect, useRef, useState } from 'react';
import { FileReferenceNode } from './file-reference-node';
import { SkillReferenceNode } from './skill-reference-node';
import { usePromptSuggestions } from './prompt-suggestions';
import type { SkillCapability } from '../lib/pulsara-types';
import { splitUserFileReferences, type CompleteWorkspacePaths } from '../lib/file-reference';

function documentFor(text: string): JSONContent {
  const content: JSONContent[] = splitUserFileReferences(text).flatMap(part => typeof part !== 'string'
    ? [{ type: 'fileReference', attrs: { raw: part.raw, name: part.name, kind: part.kind, state: 'ready' } }]
    : part.split('\n').flatMap((line, index) => [
      ...(index ? [{ type: 'hardBreak' }] : []), ...(line ? [{ type: 'text', text: line }] : []),
    ]));
  return { type: 'doc', content: [{ type: 'paragraph', content }] };
}

/** The comment shares the composer's reference nodes and suggestion owner; quote is outside this editor. */
export function ReferenceCommentEditor({ value, onChange, skills, completePaths }: {
  value: string; onChange: (value: string) => void; skills: readonly SkillCapability[]; completePaths?: CompleteWorkspacePaths;
}) {
  const change = useRef(onChange); change.current = onChange;
  const [editor] = useState(() => new Editor({
    extensions: [Document, Paragraph, Text, HardBreak, UndoRedo, FileReferenceNode, SkillReferenceNode],
    content: documentFor(value),
    editorProps: { attributes: { class: 'annotation-comment-editor', role: 'textbox', 'aria-label': '批注内容', 'aria-multiline': 'true', 'data-placeholder': '添加评论…' } },
    onUpdate: ({ editor }) => change.current(editor.getText({ blockSeparator: '\n' })),
  }));
  const keydown = usePromptSuggestions(editor, false, { skills, completePaths });
  useLayoutEffect(() => {
    editor.commands.setTextSelection(Math.max(1, editor.state.doc.content.size - 1));
    editor.view.dom.focus({ preventScroll: true }); editor.view.focus();
  }, [editor]);
  useEffect(() => {
    if (editor.getText({ blockSeparator: '\n' }) !== value) editor.commands.setContent(documentFor(value), { emitUpdate: false });
  }, [editor, value]);
  useEffect(() => {
    editor.setOptions({ editorProps: { ...editor.options.editorProps,
      handleKeyDown: (view, event) => {
        if (view.composing || event.isComposing || event.keyCode === 229) return false;
        if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === 'z') {
          event.preventDefault(); event.stopPropagation();
          if (event.shiftKey) editor.commands.redo(); else editor.commands.undo();
          return true;
        }
        if (keydown.current(view, event)) { event.preventDefault(); event.stopPropagation(); return true; }
        if (event.key === 'Enter' && !event.metaKey && !event.ctrlKey) { event.preventDefault(); editor.commands.setHardBreak(); return true; }
        return false;
      },
      handlePaste: (_view, event) => {
        const text = event.clipboardData?.getData('text/plain');
        if (text === undefined) return false;
        event.preventDefault(); editor.commands.insertContent(documentFor(text).content![0].content ?? []); return true;
      },
    } });
    return () => editor.destroy();
  }, [editor, keydown]);
  return <EditorContent editor={editor} />;
}
