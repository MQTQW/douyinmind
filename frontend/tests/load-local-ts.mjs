import { readFile } from 'node:fs/promises';
import ts from 'typescript';

const cache = new Map();
export async function localModuleURL(url) {
  const key = String(url);
  if (cache.has(key)) return cache.get(key);
  let { outputText } = ts.transpileModule(await readFile(url, 'utf8'), {
    compilerOptions: { target: ts.ScriptTarget.ES2020, module: ts.ModuleKind.ESNext, jsx: ts.JsxEmit.ReactJSX },
  });
  for (const match of [...outputText.matchAll(/from (['"])([^'"]+)\1/g)]) {
    const name = match[2];
    const resolved = name.startsWith('.')
      ? await localModuleURL(new URL(name + (/\.[cm]?[jt]sx?$/.test(name) ? '' : name.includes('components') || name === './CitationSources' ? '.tsx' : '.ts'), url))
      : import.meta.resolve(name);
    outputText = outputText.replaceAll(match[0], `from ${JSON.stringify(resolved)}`);
  }
  const result = `data:text/javascript;base64,${Buffer.from(outputText).toString('base64')}`;
  cache.set(key, result);
  return result;
}
