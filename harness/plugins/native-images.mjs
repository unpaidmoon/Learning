// Authorized document raster or frozen submitted crop -> exact model encoding.
// This private API accepts bytes from the business source store, never paths,
// URLs, frame requests, or a tool's assertion that it has permission.
import { createHash } from 'node:crypto';

const sha = value => createHash('sha256').update(value).digest('hex');
const identity = value => typeof value === 'string' && /^[A-Za-z0-9_-]{1,160}$/.test(value);
const digest = value => typeof value === 'string' && /^[a-f0-9]{64}$/.test(value);
const keys = (value, expected) => value && typeof value === 'object' && !Array.isArray(value)
  && Object.keys(value).sort().join() === expected.sort().join();

export async function prepareDocumentImages(ctx, value, policy) {
  if (!keys(value, ['session_id', 'run_id', 'request_id', 'epoch', 'provider', 'images']) || !policy
    || ['session_id', 'run_id', 'request_id', 'epoch'].some(key => value[key] !== policy[key])
    || !['deepseek-official', 'deepseek-account'].includes(value.provider)
    || !Array.isArray(value.images) || value.images.length < 1 || value.images.length > 6) {
    throw Error('DOCUMENT_IMAGE_OWNER_MISMATCH');
  }
  const signal = AbortSignal.timeout(Math.max(1, Math.ceil(Math.min(30000, policy.timeout_seconds * 1000))));
  const proofs = [], seen = new Set();
  let totalBytes = 0;
  for (const image of value.images) {
    const common = ['kind', 'source_id', 'course_id', 'source_sha256', 'media_type', 'data'];
    const region = image?.kind === 'document_region';
    const document = image?.kind === 'document_page' || region;
    const submitted = image?.kind === 'submitted_image';
    const scopeKeys = region ? ['source_version', 'source_digest', 'page_index', 'parent_text_snapshot_id', 'parent_text_hash', 'region_id', 'region_sha256']
      : document ? ['source_version', 'source_digest', 'page_index']
      : ['context_snapshot_id', 'context_hash', 'message_id', 'revision_id', 'attachment_id'];
    if (!keys(image, [...common, ...scopeKeys]) || (!document && !submitted)
      || !identity(image.source_id) || !identity(image.course_id) || !digest(image.source_sha256)
      || (document && (!identity(image.source_version) || !digest(image.source_digest)
        || !Number.isSafeInteger(image.page_index) || image.page_index < 0 || image.media_type !== 'image/png'))
      || (region && (!identity(image.parent_text_snapshot_id) || !digest(image.parent_text_hash)
        || !identity(image.region_id) || !digest(image.region_sha256)))
      || (submitted && (!digest(image.context_hash)
        || ['context_snapshot_id', 'message_id', 'revision_id', 'attachment_id'].some(key => !identity(image[key]))
        || !['image/png', 'image/jpeg', 'image/webp'].includes(image.media_type)))
      || typeof image.data !== 'string' || seen.has(image.source_id)) throw Error('DOCUMENT_IMAGE_GRANT_MISMATCH');
    seen.add(image.source_id);
    const raw = Buffer.from(image.data, 'base64');
    totalBytes += raw.length;
    if (!raw.length || totalBytes > 14 * 1024 * 1024 || raw.toString('base64') !== image.data
      || sha(raw) !== image.source_sha256) throw Error('DOCUMENT_IMAGE_SOURCE_HASH');
    signal.throwIfAborted();
    const ref = await ctx.attachments.saveImage({ data: new Uint8Array(raw), mediaType: image.media_type,
      name: document ? 'document-page.png' : `submitted-crop.${image.media_type.split('/')[1]}` });
    const normalized = await ctx.attachments.readImage(ref, signal);
    const target = ctx.llm.imageRequestTarget(value.provider, policy.model, ref);
    const encoded = await ctx.attachments.readImageRequest(ref, target, signal);
    const normalizedHash = sha(normalized.data);
    if (encoded.attachment.attachmentId !== ref.attachmentId || ref.attachmentId !== `sha256:${normalizedHash}`
      || encoded.depth !== 'uchar' || encoded.space !== 'srgb' || encoded.bytes !== encoded.data.length) {
      throw Error('DOCUMENT_IMAGE_NATIVE_IDENTITY_MISMATCH');
    }
    const { data: _data, ...grant } = image;
    proofs.push({ ...grant, session_id: value.session_id, run_id: value.run_id, request_id: value.request_id,
      epoch: value.epoch, provider: value.provider, model: policy.model,
      normalized_ref: ref, normalized_sha256: normalizedHash, target, variant_id: encoded.variantId,
      encoded_sha256: sha(encoded.data), encoded_media_type: encoded.mediaType, encoded_bytes: encoded.bytes,
      encoded_width: encoded.width, encoded_height: encoded.height, depth: encoded.depth, space: encoded.space,
      has_alpha: encoded.hasAlpha });
  }
  signal.throwIfAborted();
  return proofs;
}
