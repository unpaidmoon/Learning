/* DeepStudent 635216503c0b34bdf812e17942e0f09f51872e6f, AGPL-3.0-or-later.
 * Original selected declarations, unchanged. Source and license accompany the project.
 * Storage, snapshot truncation, navigation and upstream app imports are excluded. */
import { t } from './selectionLocale';

export type SelectionSourceKind = 'pdf' | 'epub' | 'document' | 'translation' | 'message' | 'mindmap' | 'note';

export interface SelectionRefData {
  /** 选区文本（导图面为节点 + 子树大纲文本） */
  text: string;
  source: {
    kind: SelectionSourceKind;
    /** 源资源 id（pdf/note/mindmap），用于回链跳转与 agent 工具回读 */
    sourceId?: string;
    /** 定位符：'page:N' / 导图节点 id / 笔记 section 等 */
    locator?: string;
    /** 来源显示名（文档名 / 导图标题 / 会话标题） */
    title?: string;
    /** kind=message 时的消息 id */
    messageId?: string;
  };
}

export function describeSelectionLocator(locator?: string): string | undefined {
  const match = locator?.match(/^(page|slide|chapter|section):(\d+)(?:-(\d+))?$/);
  if (!match) return undefined;
  const [, kind, from, to] = match;
  const value = to ? `${from}-${to}` : from;
  const defaults: Record<string, string> = {
    page: '第 {{value}} 页',
    slide: '第 {{value}} 张幻灯片',
    chapter: '第 {{value}} 章',
    section: '第 {{value}} 节',
  };
  return t(`selectionRef.locator.${kind}`, { value, defaultValue: defaults[kind] }, 'chatV2');
}

export function buildSelectionDisplayName(source: SelectionRefData['source']): string {
  const parts = [source.title, describeSelectionLocator(source.locator)].filter(Boolean);
  if (parts.length > 0) return parts.join(' ');
  return t('selectionRef.fallbackName', { defaultValue: '选区引用' }, 'chatV2');
}
