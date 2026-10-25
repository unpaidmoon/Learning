/** Project strict text-only wrapper over retained DS IR/Markdown functions.
 * C06 DS files remain under their archived notices; no frame/AI pipeline here.
 */
import {parseSectionBlocks, type HandoutSection} from './retained/ir';
import {handoutToMarkdown} from './retained/markdown';

export interface BoundSource {
  asset_id: string;
  source_version: string;
  source_sha256: string;
  content_revision: string;
  content_role: string;
}

export interface SectionInput {
  heading: string;
  start_ms: number;
  end_ms: number;
  ir_json: string;
}

export function renderBoundHandout(input: {source: BoundSource; title: string; summary: string; lang: 'zh'|'en'; sections: SectionInput[]}) {
  const source=input.source;
  if (!/^[A-Za-z0-9_-]{1,160}$/.test(source.asset_id) || !source.source_version || !source.content_revision || !source.content_role || !/^[a-f0-9]{64}$/.test(source.source_sha256)) throw new Error('HANDOUT_SOURCE_INVALID');
  if (!input.sections.length) throw new Error('HANDOUT_SECTIONS_EMPTY');
  const sections: HandoutSection[]=[];
  for (const section of input.sections) {
    if (!section.heading.trim() || !Number.isSafeInteger(section.start_ms) || !Number.isSafeInteger(section.end_ms) || section.start_ms<0 || section.end_ms<=section.start_ms) throw new Error('HANDOUT_RANGE_INVALID');
    const raw=JSON.parse(section.ir_json);
    if (!raw || !Array.isArray(raw.blocks) || !raw.blocks.length) throw new Error('HANDOUT_BLOCKS_INVALID');
    for (const block of raw.blocks) {
      if (!block || typeof block!=='object') throw new Error('HANDOUT_BLOCK_INVALID');
      if (block.type==='figure') throw new Error('HANDOUT_CONFIRMED_IMAGE_MAPPING_REQUIRED');
      if (['lead','para','h2','note'].includes(block.type) && (typeof block.text!=='string' || !block.text.trim())) throw new Error('HANDOUT_EMPTY_TEXT_BLOCK');
      if (block.type==='list' && (!Array.isArray(block.items) || !block.items.length || block.items.some((x:unknown)=>typeof x!=='string'||!x.trim()))) throw new Error('HANDOUT_EMPTY_LIST_ITEM');
    }
    const blocks=parseSectionBlocks(JSON.stringify(raw),{startSec:section.start_ms/1000,endSec:section.end_ms/1000});
    if (blocks.length!==raw.blocks.length) throw new Error('HANDOUT_BLOCK_WAS_DROPPED');
    sections.push({heading:section.heading,startSec:section.start_ms/1000,blocks});
  }
  const markdown=handoutToMarkdown({resourceId:source.asset_id,title:input.title,summary:input.summary,sections,lang:input.lang});
  return {markdown,sections,source:{...source},section_locators:input.sections.map((s,i)=>({section_index:i,start_ms:s.start_ms,end_ms:s.end_ms,source_version:source.source_version,content_revision:source.content_revision}))};
}
