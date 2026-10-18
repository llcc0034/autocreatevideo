// Read-only documentation check. No backend, browser, database, or model calls.
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

export function markdownLinks(text) {
  const visible=text.replace(/^(`{3,}|~{3,})[^\n]*\n[\s\S]*?^\1\s*$/gm,'');
  return [...visible.matchAll(/!?\[[^\]\n]*\]\(([^\s)]+)\)/g)].map(m=>m[1]);
}
export function headingIds(text) {
  const ids=new Set([...text.matchAll(/<a\s+(?:id|name)="([^"]+)"><\/a>/g)].map(m=>m[1]));
  const counts=new Map();
  const visible=text.replace(/^(`{3,}|~{3,})[^\n]*\n[\s\S]*?^\1\s*$/gm,'');
  for(const m of visible.matchAll(/^#{1,6} (.+)$/gm)) {
    const key=m[1].trim().toLowerCase().replace(/<[^>]*>/g,'').replace(/[\p{P}\p{S}]/gu,c=>c==='-'||c==='_'?c:'').replace(/ /g,'-');
    const n=counts.get(key)??0;counts.set(key,n+1);ids.add(key+(n?'-'+n:''));
  }
  return ids;
}
export function resolveLink(file,href) {
  if(/^(?:[a-z][\w+.-]*:|\/)/i.test(href))return null;
  const [pathname,fragment]=href.split('#');
  const target=pathname?path.posix.normalize(path.posix.join(path.posix.dirname(file),decodeURIComponent(pathname.split('?')[0]))):file;
  return {target:target.replace(/:\d+(?:-\d+)?$/,''),fragment:fragment?decodeURIComponent(fragment):''};
}
export function checkLinks(root,files,{onlyMarkdown=true}={}) {
  const issues=[];let checked=0;const cache=new Map();
  for(const file of files)for(const href of markdownLinks(fs.readFileSync(path.join(root,file),'utf8'))) {
    const ref=resolveLink(file,href);if(!ref)continue;
    if(onlyMarkdown&&!ref.target.endsWith('.md'))continue;
    checked++;
    if(!fs.existsSync(path.join(root,ref.target)))issues.push({file,href,...ref,reason:'file_missing'});
    else if(ref.fragment&&ref.target.endsWith('.md')) {
      if(!cache.has(ref.target))cache.set(ref.target,headingIds(fs.readFileSync(path.join(root,ref.target),'utf8')));
      if(!cache.get(ref.target).has(ref.fragment))issues.push({file,href,...ref,reason:'anchor_missing'});
    }
  }
  return {checked,issues};
}
export function taskRows(text) {
  return [...text.matchAll(/^- \[([ xX])\] (?:\*\*)?((?:M\d|CL|INT)-\d{2}(?:-G\d{2})?|G\d{2}|UI-H\d{2})\b([^\n]*)/gm)].map(m=>({id:m[2],checked:m[1].toLowerCase()==='x',line:m[0]}));
}
export function checkFrontendChecklist(text) {
  const errors=[];
  const visible=text.replace(/^(`{3,}|~{3,})[^\n]*\n[\s\S]*?^\1\s*$/gm,'');
  const begin=/^## 任务清单\r?$/m.exec(visible);
  const end=/^## 设计与验收标准\r?$/m.exec(visible);
  if(!begin||!end||begin.index>=end.index)errors.push('UI checklist: missing or reversed task/design sections');
  const checks=[...visible.matchAll(/^[ \t]*(?:[-+*]|\d+[.)])\s+\[[ xX]\]\s+[^\n]*/gm)];
  const expected=Array.from({length:12},(_,i)=>`UI-H${String(i+1).padStart(2,'0')}`);
  const actual=[];
  for(const check of checks) {
    const id=/^- \[[ xX]\] (UI-H\d{2})\b/.exec(check[0]);
    if(!id||!expected.includes(id[1]))errors.push(`UI checklist: only top-level UI-H01..12 tasks may use checkboxes: ${check[0].trim()}`);
    if(id)actual.push(id[1]);
    if(begin&&end&&(check.index<begin.index||check.index>=end.index))errors.push('UI checklist: task checkbox outside task section');
  }
  if(JSON.stringify(actual)!==JSON.stringify(expected))errors.push('UI checklist: each of UI-H01..12 must occur once in order');
  if(begin&&end&&/18\.14\.\d/.test(visible.slice(begin.index,end.index)))errors.push('UI checklist: move implementation numbers to evidence records; use semantic link labels');
  return errors;
}
export function walkMarkdown(root,dir) {
  return fs.readdirSync(path.join(root,dir),{withFileTypes:true}).flatMap(e=>e.isDirectory()?walkMarkdown(root,dir+'/'+e.name):e.name.endsWith('.md')?[dir+'/'+e.name]:[]);
}
export function audit(root) {
  const files=walkMarkdown(root,'docs/optimization');
  const links=checkLinks(root,files);
  const modules=files.filter(f=>f.includes('/modules/'));
  const rows=modules.flatMap(f=>taskRows(fs.readFileSync(path.join(root,f),'utf8')).map(t=>({...t,file:f})));
  const ids=new Map();const errors=[];
  errors.push(...checkFrontendChecklist(fs.readFileSync(path.join(root,'docs/optimization/modules/frontend-hierarchy/design.md'),'utf8')));
  for(const row of rows) {if(ids.has(row.id))errors.push(`Duplicate task ${row.id}`);ids.set(row.id,row);}
  for(const [file,max] of [['docs/optimization/README.md',8192],['docs/optimization/current.md',8192],['docs/optimization/module-development-detailed-design-2026-09-28.md',8192],['docs/optimization/execution-instance-unification-plan.md',8192]]) {
    if(fs.statSync(path.join(root,file)).size>max)errors.push(`Resume/index is oversized: ${file}`);
    if(taskRows(fs.readFileSync(path.join(root,file),'utf8')).length)errors.push(`Index duplicates task checklist: ${file}`);
  }
  const groups={main:rows.filter(r=>/^(?:M\d|CL|INT)-\d{2}$/.test(r.id)),g:rows.filter(r=>/(?:^|-)G\d{2}$/.test(r.id)),ui:rows.filter(r=>/^UI-/.test(r.id))};
  for(const [group,count] of [['main',91],['g',6],['ui',12]])if(groups[group].length!==count)errors.push(`${group}: expected ${count} task rows, got ${groups[group].length}`);
  return {files:files.length,links:links.checked,issues:links.issues,errors,tasks:Object.fromEntries(Object.entries(groups).map(([k,v])=>[k,{total:v.length,completed:v.filter(r=>r.checked).length}]))};
}
if(process.argv[1]&&fileURLToPath(import.meta.url)===path.resolve(process.argv[1])) {
  const root=path.resolve(path.dirname(fileURLToPath(import.meta.url)),'..');
  const result=audit(root);console.log(JSON.stringify(result,null,2));
  if(result.errors.length||result.issues.length)process.exitCode=1;
}
