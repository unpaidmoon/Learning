// Closed-profile maintenance only. Preserve the immutable native header and
// append the pinned working-directory event, without queuing model input.
import path from 'node:path';

export function workspaceRelocator(record, workspace) {
  if (record === null) return () => false;
  if (!record || Object.keys(record).sort().join() !== 'format,origins,target'
    || record.format !== 1 || typeof record.target !== 'string'
    || !path.isAbsolute(record.target) || path.resolve(record.target) !== path.resolve(workspace)
    || !Array.isArray(record.origins) || !record.origins.length || record.origins.length > 100
    || record.origins.some(value => typeof value !== 'string' || !path.isAbsolute(value)
      || path.resolve(value) === path.resolve(workspace))) throw Error('LEARNING_WORKSPACE_RELOCATION_INVALID');
  const origins = new Set(record.origins.map(value => path.resolve(value)));
  return (agent, workingDirectory) => {
    const previous = workingDirectory.get(agent.session);
    if (!origins.has(path.resolve(previous))) return false;
    agent.session.append('working-directory/change', { cwd: path.resolve(workspace) });
    return true;
  };
}
