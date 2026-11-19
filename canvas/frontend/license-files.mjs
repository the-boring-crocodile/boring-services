// One notice per package/version/name and byte content, independent of npm nesting.
import { writeFile } from 'node:fs/promises';
import { createHash } from 'node:crypto';
import { resolve } from 'node:path';

export function licenseWriter(directory) {
  const written = new Map();
  return async (name, version, notice, content) => {
    const base = name.replaceAll('/', '__') + '@' + version + '__' + notice;
    let filename = base;
    if (written.has(base) && !written.get(base).equals(content))
      filename += '__' + createHash('sha256').update(content).digest('hex').slice(0, 12);
    if (filename.length > 100)
      throw Error('licence filename exceeds 100 characters: ' + filename);
    const previous = written.get(filename);
    if (previous && !previous.equals(content))
      throw Error('licence filename collision: ' + filename);
    if (!previous) {
      await writeFile(resolve(directory, filename), content);
      written.set(filename, content);
    }
    return 'licenses/' + filename;
  };
}
