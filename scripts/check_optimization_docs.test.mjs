import test from 'node:test';
import assert from 'node:assert/strict';
import { markdownLinks, headingIds, resolveLink, taskRows, checkFrontendChecklist } from './check_optimization_docs.mjs';

test('extract links without mistaking fenced examples for documentation links',()=>{
  assert.deepEqual(markdownLinks('[ok](current.md)\n```md\n[example](missing.md)\n```\n![image](a.png)'),['current.md','a.png']);
});
test('recognize stable anchors and duplicate heading suffixes',()=>{
  const ids=headingIds('<a id="m1"></a>\n## A title\n## A title\n#### 18.14.28 映射（2026-10-10）');
  assert(ids.has('m1'));assert(ids.has('a-title'));assert(ids.has('a-title-1'));assert(ids.has('181428-映射2026-10-10'));
});
test('resolve moved relative paths, fragments and line-qualified source links',()=>{
  assert.deepEqual(resolveLink('docs/optimization/modules/m1/design.md','../../current.md#next'),{target:'docs/optimization/current.md',fragment:'next'});
  assert.deepEqual(resolveLink('docs/a.md','../frontend/x.ts:12'),{target:'frontend/x.ts',fragment:''});
  assert.equal(resolveLink('docs/a.md','https://example.com/a'),null);
});
test('extract original task identities and completion without counting references',()=>{
  assert.deepEqual(taskRows('- [x] **M1-01 任务**\n- [ ] UI-H12 输入\n参见M1-01').map(({id,checked})=>({id,checked})),[{id:'M1-01',checked:true},{id:'UI-H12',checked:false}]);
});
test('keep original G subtask identities distinct from parent task',()=>{
  assert.deepEqual(taskRows('- [x] **M1-08 父项**\n- [x] **M1-08-G01 子项**').map(({id})=>id),['M1-08','M1-08-G01']);
});

const uiTasks=Array.from({length:12},(_,i)=>`- [${[0,1,2,9].includes(i)?'x':' '}] UI-H${String(i+1).padStart(2,'0')} 任务`).join('\n');
const uiDocument=`## 任务清单\n\n${uiTasks}\n\n## 设计与验收标准\n\n#### 18.14.1 原设计\n\n## 按功能查实施记录\n\n| 18.14.41 | [公共返回](records.md#181441-返回) |\n`;

test('UI authority keeps twelve tasks and allows historical numbers outside the checklist',()=>{
  assert.deepEqual(checkFrontendChecklist(uiDocument),[]);
  assert.deepEqual(checkFrontendChecklist(uiDocument.replaceAll('\n','\r\n')),[]);
  assert.equal(taskRows(uiDocument).filter(row=>row.checked).length,4);
});

test('UI guard rejects hidden nested or extra implementation checkboxes',()=>{
  for(const extra of ['  - [x] 18.14.41 子包','- [ ] 18.14.46 新批次','  * [x] 子包','1. [ ] 子包','  - [x] UI-H04 子包']) {
    assert(checkFrontendChecklist(uiDocument.replace('## 设计与验收标准',`${extra}\n\n## 设计与验收标准`)).some(error=>error.includes('only top-level')),extra);
  }
});

test('UI guard rejects missing, duplicated or substituted task IDs even with the same count',()=>{
  for(const changed of [uiDocument.replace('UI-H12','UI-H13'),uiDocument.replace('UI-H12','UI-H11'),uiDocument.replace('- [ ] UI-H12 任务\n','')]) {
    assert(checkFrontendChecklist(changed).some(error=>error.includes('once in order')));
  }
});

test('UI guard does not allow task checkboxes to drift into design or evidence sections',()=>{
  const task='- [ ] UI-H12 任务';
  assert(checkFrontendChecklist(uiDocument.replace(task,'')+task).some(error=>error.includes('outside task section')));
});

test('UI guard requires explicit task/design boundaries and semantic evidence labels',()=>{
  assert(checkFrontendChecklist(uiDocument.replace('## 任务清单','## 零散记录')).some(error=>error.includes('sections')));
  assert(checkFrontendChecklist(uiDocument.replace('UI-H04 任务','UI-H04 任务（18.14.41）')).some(error=>error.includes('implementation numbers')));
  assert.deepEqual(checkFrontendChecklist(uiDocument.replace('UI-H04 任务','UI-H04 任务 [公共返回](records.md#181441-返回)')),[]);
});

test('UI guard ignores fenced documentation examples instead of counting them as tasks',()=>{
  assert.deepEqual(checkFrontendChecklist(uiDocument+'\n```md\n- [x] 18.14.99 示例\n```\n'),[]);
});
