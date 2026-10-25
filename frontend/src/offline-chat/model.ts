export type ChatAttachment = {
  filename: string;
  media_type: string;
  path: string;
};

export type SavedMessage = {
  id: string;
  role: string;
  text: string;
  status: string;
  created_at: string;
  reasoning?: string;
  sources: string[];
  attachments: ChatAttachment[];
};

export type OfflineChatPage = {
  format: "learning-offline-chat-v1";
  course: string;
  title: string;
  messages: SavedMessage[];
  navigation?: {
    previous?: string | null;
    next?: string | null;
    first: number;
    last: number;
    total: number;
  };
};

export type OfflineChatIndex = {
  format: "learning-offline-chat-index-v1";
  exported_at: string;
  courses: { id: string; title: string; chats: { title: string; path: string }[] }[];
};

export function localAttachment(path: string) {
  const object = /^\.\.\/\.\.\/data\/attachments\/objects\/([a-f0-9]{2})\/([a-f0-9]{64})$/.exec(
    path,
  );
  if (object && object[1] === object[2].slice(0, 2)) return path;
  if (/^\.\.\/\.\.\/data\/learning-source-images\/[a-f0-9]{64}\.png$/.test(path)) return path;
  return undefined;
}

export function localChat(path: string) {
  return /^chats\/[A-Za-z0-9_-]{1,160}\.html$/.test(path) ? path : undefined;
}

export function localSibling(path?: string | null) {
  return path && /^[A-Za-z0-9_-]{1,170}\.html$/.test(path) ? path : undefined;
}
