import React, {useCallback, useEffect, useMemo, useRef, useState} from "react";

const PAGE_SIZE = 20;
const REFRESH_MS = 3000;
const RUNNING = new Set(["preparing", "rendering", "verifying"]);
const NEEDS_ACTION = new Set(["stalled", "paused", "needs_action", "needs_manual_action", "interrupted"]);
const LABELS = {
  queued:"排队中", preparing:"生成中", rendering:"生成中", verifying:"生成中",
  succeeded:"已完成", completed:"已完成", failed:"失败", stalled:"需要处理",
  paused:"需要处理", needs_action:"需要处理", needs_manual_action:"需要处理",
  interrupted:"需要处理", cancelled:"已取消",
};
const STAGE_LABELS = {preparing:"准备任务",materials:"准备素材",matching:"匹配素材",tts:"生成配音",subtitle:"生成字幕",rendering:"视频渲染",merging:"合并成片",verifying:"校验成片",failed:"任务执行"};

function label(status){return LABELS[status]||status||"排队中"}
function stageLabel(stage){return STAGE_LABELS[stage]||stage||"—"}
function statusClass(status){
  if(RUNNING.has(status))return "running";
  if(NEEDS_ACTION.has(status))return "needs-action";
  if(status==="failed")return "failed";
  if(["succeeded","completed"].includes(status))return "succeeded";
  if(status==="cancelled")return "cancelled";
  return "queued";
}
function taskNumber(task){
  if(task.index_no&&task.batch_total)return `${String(task.index_no).padStart(3,"0")}/${String(task.batch_total).padStart(3,"0")}`;
  const match=String(task.task_name||"").match(/(\d{3})\/(\d{3})/);
  return match?`${match[1]}/${match[2]}`:"单条";
}
function taskProject(task){return task.project_name||String(task.task_name||"未命名任务").split("·")[0].trim()}
function taskTitle(task){return `${taskProject(task)} · ${taskNumber(task)}`}
function compactTime(value){return value?String(value).replace("T"," ").slice(5,16):"—"}
function duration(task){
  if(!task?.started_at)return "—";
  const start=new Date(String(task.started_at).replace(" ","T")).getTime();
  const interrupted=["failed","stalled","paused","needs_action","needs_manual_action","interrupted"].includes(task.status);
  const endRaw=interrupted?(task.last_heartbeat_at||task.finished_at||task.updated_at):(task.finished_at||task.updated_at);
  const end=endRaw?new Date(String(endRaw).replace(" ","T")).getTime():Date.now();
  if(!Number.isFinite(start)||!Number.isFinite(end))return "—";
  const seconds=Math.max(0,Math.round((end-start)/1000));
  if(seconds<60)return `${seconds}秒`;
  if(seconds<3600)return `${Math.floor(seconds/60)}分${seconds%60}秒`;
  return `${Math.floor(seconds/3600)}时${Math.floor((seconds%3600)/60)}分`;
}
function queryString(values){
  const params=new URLSearchParams();
  Object.entries(values).forEach(([key,value])=>{if(value!==""&&value!==0&&value!=null)params.set(key,String(value))});
  return params.toString();
}

function ConfirmDialog({title,children,confirmText="确定",danger=false,onCancel,onConfirm}){
  return <div className="flat-task-modal-backdrop" onMouseDown={onCancel}>
    <section className="flat-task-modal" onMouseDown={event=>event.stopPropagation()}>
      <header><h2>{title}</h2><button onClick={onCancel}>×</button></header>
      <div className="flat-task-modal-body">{children}</div>
      <footer><button onClick={onCancel}>取消</button><button className={danger?"danger":"primary"} onClick={onConfirm}>{confirmText}</button></footer>
    </section>
  </div>;
}

function TaskDetail({task,detail,request,notify,onRefresh,onLog,onDelete,onDeleteContinue}){
  if(!task)return <aside className="flat-task-detail"><header>任务详情</header><div className="flat-detail-empty">选择一条任务查看详情</div></aside>;
  const data={...task,...detail};
  const logs=String(data.recent_logs||"").split(/\r?\n/).filter(Boolean).slice(-5);
  const action=async path=>{try{await request(path,{method:"POST",body:{}});await onRefresh()}catch(error){notify(error.message)}};
  return <aside className="flat-task-detail">
    <header>任务详情</header>
    <section className="flat-detail-heading"><div><b>{taskProject(data)}</b><span>{taskNumber(data)}</span></div><i className={`flat-status ${statusClass(data.status)}`}>{label(data.status)}</i></section>
    <dl className="flat-detail-facts">
      <div><dt>来源批次</dt><dd>{data.batch_name||"单条生成"}</dd></div>
      <div><dt>进度</dt><dd>{Number(data.progress||0)}%</dd></div>
      {data.failed_stage&&<div><dt>失败阶段</dt><dd>{stageLabel(data.failed_stage)}</dd></div>}
      {(data.error_summary||data.error_message)&&<div className="error"><dt>失败原因</dt><dd>{data.error_summary||data.error_message}</dd></div>}
      <div><dt>当前步骤</dt><dd>{data.current_step||data.display_step||"—"}</dd></div>
      <div><dt>开始时间</dt><dd>{compactTime(data.started_at||data.created_at)}</dd></div>
      <div><dt>耗时</dt><dd>{duration(data)}</dd></div>
      <div><dt>输出文件</dt><dd title={data.output_path||""}>{data.output_path||"—"}</dd></div>
    </dl>
    <section className="flat-detail-logs"><b>最近日志</b>{logs.length?logs.map((line,index)=><p key={index}>{line}</p>):<p>暂无日志</p>}<button onClick={()=>onLog(data)}>查看完整日志</button></section>
    <footer className="flat-detail-actions">
      {RUNNING.has(data.status)&&<button onClick={()=>action(`/api/tasks/${data.id}/cancel`)}>停止任务</button>}
      {data.status==="queued"&&<button onClick={()=>action(`/api/tasks/${data.id}/cancel`)}>取消排队</button>}
      {(data.status==="failed"||NEEDS_ACTION.has(data.status))&&<><button onClick={()=>action(`/api/tasks/${data.id}/retry`)}>重新生成</button>{data.batch_id&&<><button onClick={()=>action(`/api/tasks/${data.id}/skip-and-continue`)}>跳过并继续</button><button className="danger" onClick={()=>onDeleteContinue(data)}>删除并继续</button></>}</>}
      {["succeeded","completed"].includes(data.status)&&data.output_exists&&<button className="primary" onClick={()=>action(`/api/dashboard/tasks/${data.id}/open-output`)}>打开成片/目录</button>}
      {!RUNNING.has(data.status)&&<button className="danger" onClick={()=>onDelete(data)}>删除任务</button>}
    </footer>
  </aside>;
}

function TaskLogDialog({data,onClose}){
  if(!data)return null;
  return <div className="flat-task-modal-backdrop" onMouseDown={onClose}><section className="flat-task-modal log" onMouseDown={event=>event.stopPropagation()}>
    <header><div><h2>任务日志</h2><span>{data.task_name}</span></div><button onClick={onClose}>×</button></header>
    <div className="flat-task-log"><pre>{data.recent_logs||data.diagnostic_log||"暂无日志"}</pre></div>
    <footer><button onClick={onClose}>关闭</button></footer>
  </section></div>;
}

function CleanupDialog({request,notify,onClose,onDone}){
  const [selected,setSelected]=useState(new Set(["failed","cancelled","stalled"]));
  const [age,setAge]=useState("30"),[deleteFiles,setDeleteFiles]=useState(false);
  const toggle=value=>setSelected(current=>{const next=new Set(current);next.has(value)?next.delete(value):next.add(value);return next});
  const submit=async()=>{try{const statuses=[...selected].flatMap(value=>value==="stalled"?["stalled","paused","needs_action","needs_manual_action","interrupted"]:[value]);const result=await request("/api/tasks/cleanup-records",{method:"POST",body:{statuses:[...new Set(statuses)],age,delete_outputs:deleteFiles}});notify(`已清理 ${result.deleted.length} 条任务记录`);onDone();onClose()}catch(error){notify(error.message)}};
  return <ConfirmDialog title="清理任务记录" confirmText="清理记录" danger onCancel={onClose} onConfirm={submit}>
    <p className="flat-clean-note">默认只删除任务记录，不删除已经生成的成片。</p>
    <div className="flat-clean-options">
      {[['succeeded','已完成记录'],['failed','失败记录'],['cancelled','已取消记录'],['stalled','需要处理记录']].map(([value,text])=><label key={value}><input type="checkbox" checked={selected.has(value)} onChange={()=>toggle(value)}/>{text}</label>)}
    </div>
    <label className="flat-clean-field">时间范围<select value={age} onChange={event=>setAge(event.target.value)}><option value="7">7天以前</option><option value="30">30天以前</option><option value="all">全部时间</option></select></label>
    <label className="flat-delete-file"><input type="checkbox" checked={deleteFiles} onChange={event=>setDeleteFiles(event.target.checked)}/>同时删除本地成片文件</label>
  </ConfirmDialog>;
}

function InvalidDialog({audit,request,notify,onClose,onDone}){
  const submit=async()=>{try{const result=await request("/api/tasks/cleanup-invalid",{method:"POST",body:{}});notify(`已修复 ${result.fixed} 条无效状态`);onDone();onClose()}catch(error){notify(error.message)}};
  return <ConfirmDialog title="清理失效任务" confirmText="修复失效状态" onCancel={onClose} onConfirm={submit}>
    <p className="flat-clean-note">扫描只修复失效状态和队列锁，不会删除真实成片。</p>
    <div className="flat-audit-count">发现 <b>{audit.count||0}</b> 条异常记录</div>
    <ul className="flat-audit-list">{Object.entries(audit.summary||{}).map(([key,count])=><li key={key}>{({worker_missing:"Worker已经退出",project_missing:"项目已经不存在",output_missing:"输出文件已经不存在",batch_item_missing:"批次任务关系不完整"})[key]||key}<b>{count}</b></li>)}</ul>
  </ConfirmDialog>;
}

export default function TasksPage({db,notify,request,Button,Icon,focusTaskId=0,onSummary}){
  const [tab,setTab]=useState("current"),[page,setPage]=useState(1);
  const [payload,setPayload]=useState({items:[],total:0,total_pages:1,queue:{queued_count:0}});
  const [status,setStatus]=useState(""),[projectId,setProjectId]=useState(0),[batchId,setBatchId]=useState(0),[keyword,setKeyword]=useState("");
  const [selected,setSelected]=useState(new Set()),[current,setCurrent]=useState(null),[detail,setDetail]=useState(null);
  const [dialog,setDialog]=useState(null),[log,setLog]=useState(null),[loading,setLoading]=useState(false);
  const handledFocus=useRef(0),loadSeq=useRef(0),activeTab=useRef("current");
  const load=useCallback(async(viewOverride=tab,pageOverride=page)=>{
    const targetView=typeof viewOverride==="string"?viewOverride:tab;
    const targetPage=Number.isFinite(Number(pageOverride))?Number(pageOverride):page;
    if(targetView!==activeTab.current)return null;
    const seq=++loadSeq.current;
    const query=queryString({view:targetView,page:targetPage,page_size:PAGE_SIZE,status,project_id:projectId,batch_id:batchId,keyword,focus_task_id:targetView==="current"?Number(focusTaskId||0):0});
    try{const data=await request(`/api/task-center/tasks?${query}`);if(seq!==loadSeq.current||targetView!==activeTab.current)return null;setPayload(data);onSummary?.({...data.summary,total_count:Object.values(data.summary||{}).reduce((sum,value)=>sum+Number(value||0),0)});setCurrent(previous=>previous?data.items.find(item=>item.id===previous.id)||null:null);return data}catch(error){if(seq===loadSeq.current&&targetView===activeTab.current)notify(error.message);return null}
  },[tab,page,status,projectId,batchId,keyword,focusTaskId,request,notify,onSummary]);
  useEffect(()=>{load();if(tab!=="current")return;const timer=setInterval(load,REFRESH_MS);return()=>clearInterval(timer)},[load,tab]);
  const selectTask=async task=>{setCurrent(task);setDetail(null);try{setDetail(await request(`/api/tasks/${task.id}/logs`))}catch(error){notify(error.message)}};
  useEffect(()=>{const target=Number(focusTaskId||0);if(!target||handledFocus.current===target)return;const task=(payload.items||[]).find(item=>Number(item.id)===target);if(task){handledFocus.current=target;selectTask(task)}},[focusTaskId,payload.items]);
  const refresh=async()=>{setLoading(true);await load();if(current)await selectTask(current);setLoading(false)};
  const summary=useMemo(()=>({
    running:Number(payload.summary?.running_count||0),
    queued:Number(payload.summary?.queued_count||0),
    failed:Number(payload.summary?.failed_count||0),
    needs:Number(payload.summary?.needs_action_count||0),
  }),[payload.summary]);
  const batches=useMemo(()=>Array.from(new Map((db.batch_tasks||[]).map(item=>[item.id,item])).values()),[db.batch_tasks]);
  const toggleSelected=id=>setSelected(currentSet=>{const next=new Set(currentSet);next.has(id)?next.delete(id):next.add(id);return next});
  const bulkDelete=async(ids,deleteFiles=false)=>{try{const result=await request("/api/tasks/bulk-delete",{method:"POST",body:{task_ids:ids,delete_outputs:deleteFiles}});notify(`已删除 ${result.deleted.length} 条记录`);setSelected(new Set());setDialog(null);await refresh()}catch(error){notify(error.message)}};
  const bulkCancel=async()=>{for(const id of selected){const item=payload.items.find(row=>row.id===id);if(item?.status==="queued")try{await request(`/api/tasks/${id}/cancel`,{method:"POST",body:{}})}catch(error){notify(error.message)}}setSelected(new Set());await refresh()};
  const showLogs=async task=>{try{setLog(await request(`/api/tasks/${task.id}/logs`))}catch(error){notify(error.message)}};
  const repair=async()=>{try{const result=await request("/api/generation-queue/repair",{method:"POST",body:{}});notify(result.message);await refresh()}catch(error){notify(error.message)}};
  const openAudit=async()=>{try{setDialog({type:"invalid",audit:await request("/api/tasks/invalid-scan")})}catch(error){notify(error.message)}};
  const queue=payload.queue||{};
  const running=queue.running_task, blocker=queue.blocking_task, next=queue.next_task;
  return <div className="flat-task-page">
    <header className="flat-task-page-head"><div><h1>任务中心</h1><p>每条视频按顺序生成，当前只运行一个任务</p></div><div><Button quiet onClick={refresh}>{loading?"刷新中":"刷新"}</Button><Button quiet onClick={openAudit}>清理失效任务</Button><Button quiet onClick={()=>setDialog({type:"cleanup"})}>清理任务</Button></div></header>
    <section className={`flat-current-card ${running?"running":blocker?"blocked":queue.queued_count?"warning":"idle"}`}>
      {running?<><div><span>当前正在生成</span><h2>{taskTitle(running)}</h2><p>{running.current_step||"准备生成"}</p></div><strong>{Number(running.progress||0)}%</strong><div className="flat-current-next"><span>下一条</span><b>{next?taskTitle(next):"队列末尾"}</b></div></>:
       blocker?<><div><span>队列已暂停</span><h2>阻塞任务：{taskTitle(blocker)}</h2><p>{queue.blocking_reason||"任务需要人工处理"}</p></div><div className="flat-block-actions"><button onClick={()=>selectTask(blocker)}>查看原因</button><button onClick={()=>request(`/api/tasks/${blocker.id}/retry`,{method:"POST",body:{}}).then(refresh).catch(error=>notify(error.message))}>重新生成</button><button onClick={()=>request(`/api/tasks/${blocker.id}/skip-and-continue`,{method:"POST",body:{}}).then(refresh).catch(error=>notify(error.message))}>跳过并继续</button></div></>:
       queue.queued_count?<><div><span>队列异常</span><h2>当前有 {queue.queued_count} 条任务排队，但没有生成任务运行</h2><p>{queue.blocking_reason}</p></div><button className="flat-repair" onClick={repair}>修复队列</button></>:
       <div><span>当前生成</span><h2>当前没有正在生成的视频</h2><p>创建任务后会按顺序进入这里。</p></div>}
    </section>
    <nav className="flat-task-tabs"><button className={tab==="current"?"active":""} onClick={()=>{activeTab.current="current";loadSeq.current+=1;setTab("current");setPage(1);load("current",1)}}>当前任务</button><button className={tab==="recent"?"active":""} onClick={()=>{activeTab.current="recent";loadSeq.current+=1;setTab("recent");setPage(1);load("recent",1)}}>最近完成</button><button className={tab==="history"?"active":""} onClick={()=>{activeTab.current="history";loadSeq.current+=1;setTab("history");setPage(1);load("history",1)}}>历史记录</button></nav>
    {tab==="current"&&<section className="flat-task-summary"><span><b>{summary.running}</b>生成中</span><span><b>{summary.queued}</b>排队中</span><span className="danger"><b>{summary.failed}</b>失败</span><span className="warning"><b>{summary.needs}</b>需处理</span></section>}
    <section className="flat-task-tools">
      <select value={status} onChange={event=>{setStatus(event.target.value);setPage(1)}}><option value="">全部状态</option><option value="running">生成中</option><option value="queued">排队中</option><option value="failed">失败</option><option value="needs_action">需要处理</option><option value="succeeded">已完成</option><option value="cancelled">已取消</option></select>
      <select value={projectId} onChange={event=>{setProjectId(Number(event.target.value));setPage(1)}}><option value="0">全部项目</option>{(db.templates||[]).map(item=><option key={item.id} value={item.id}>{item.name}</option>)}</select>
      <select value={batchId} onChange={event=>{setBatchId(Number(event.target.value));setPage(1)}}><option value="0">全部批次</option>{batches.map(item=><option key={item.id} value={item.id}>{item.name}</option>)}</select>
      <input value={keyword} onChange={event=>{setKeyword(event.target.value);setPage(1)}} placeholder="搜索任务或批次"/>
      {selected.size>0&&<div className="flat-selected-actions"><b>已选择 {selected.size} 条</b><button onClick={bulkCancel}>取消排队</button><button className="danger" onClick={()=>setDialog({type:"delete",ids:[...selected]})}>删除记录</button></div>}
    </section>
    <div className="flat-task-layout"><section className="flat-task-table">
      <div className="flat-task-table-head"><span/><span>任务</span><span>进度</span><span>当前步骤</span><span>耗时</span><span>状态</span><span>操作</span></div>
      {(payload.items||[]).map(task=><article key={task.id} className={`flat-task-row ${current?.id===task.id?"selected":""} ${task.is_blocking?"blocking":""}`} onClick={()=>selectTask(task)}>
        <input aria-label="选择任务" type="checkbox" checked={selected.has(task.id)} onClick={event=>event.stopPropagation()} onChange={()=>toggleSelected(task.id)}/>
        <div className="flat-task-name"><b>{taskTitle(task)}</b><span>{task.batch_name?`批次：${task.batch_name}`:"单条生成"}{task.is_next?" · 下一条":""}</span>{(task.status==="failed"||NEEDS_ACTION.has(task.status))&&<em>{task.error_summary||"任务执行异常，请查看详情"}</em>}</div>
        <strong className="flat-task-progress">{Number(task.progress||0)}%</strong>
        <span className="flat-task-step">{task.display_step}</span><time>{duration(task)}</time><i className={`flat-status ${statusClass(task.status)}`}>{label(task.status)}</i>
        <div className="flat-row-actions" onClick={event=>event.stopPropagation()}>
          {RUNNING.has(task.status)&&<button onClick={()=>request(`/api/tasks/${task.id}/cancel`,{method:"POST",body:{}}).then(refresh).catch(error=>notify(error.message))}>停止</button>}
          {task.status==="queued"&&<button onClick={()=>request(`/api/tasks/${task.id}/cancel`,{method:"POST",body:{}}).then(refresh).catch(error=>notify(error.message))}>取消</button>}
          {(task.status==="failed"||NEEDS_ACTION.has(task.status))&&<><button onClick={()=>selectTask(task)}>查看原因</button><button onClick={()=>request(`/api/tasks/${task.id}/retry`,{method:"POST",body:{}}).then(refresh).catch(error=>notify(error.message))}>重试</button></>}
          {["succeeded","completed"].includes(task.status)&&task.output_exists&&<button onClick={()=>request(`/api/dashboard/tasks/${task.id}/open-output`,{method:"POST",body:{}}).catch(error=>notify(error.message))}>打开成片</button>}
          {!RUNNING.has(task.status)&&<button className="danger" onClick={()=>setDialog({type:"delete",ids:[task.id]})}>删除</button>}
        </div>
      </article>)}
      {!payload.items?.length&&<div className="flat-task-empty">{tab==="current"?"当前没有等待或异常任务":tab==="recent"?"最近还没有完成的视频":"没有找到符合条件的历史任务"}</div>}
      {tab==="history"&&payload.total>0&&<footer className="flat-task-pagination"><span>共 {payload.total} 条</span><div><button disabled={page<=1} onClick={()=>setPage(value=>value-1)}>上一页</button><b>{page}/{payload.total_pages}</b><button disabled={page>=payload.total_pages} onClick={()=>setPage(value=>value+1)}>下一页</button></div></footer>}
    </section><TaskDetail task={current} detail={detail} request={request} notify={notify} onRefresh={refresh} onLog={showLogs} onDelete={task=>setDialog({type:"delete",ids:[task.id]})} onDeleteContinue={task=>setDialog({type:"deleteContinue",task})}/></div>
    {dialog?.type==="delete"&&<ConfirmDialog title="删除任务记录" confirmText="删除记录" danger onCancel={()=>setDialog(null)} onConfirm={()=>bulkDelete(dialog.ids,Boolean(dialog.deleteFiles))}><p>删除任务记录不会删除已生成的成片。</p><label className="flat-delete-file"><input type="checkbox" checked={Boolean(dialog.deleteFiles)} onChange={event=>setDialog({...dialog,deleteFiles:event.target.checked})}/>同时删除本地成片文件</label><small>共选择 {dialog.ids.length} 条任务</small></ConfirmDialog>}
    {dialog?.type==="deleteContinue"&&<ConfirmDialog title="删除并继续" confirmText="删除并继续" danger onCancel={()=>setDialog(null)} onConfirm={async()=>{try{await request(`/api/tasks/${dialog.task.id}/delete-and-continue`,{method:"POST",body:{}});setDialog(null);notify("失败任务已归档，队列继续执行");await refresh()}catch(error){notify(error.message)}}}><p>确定跳过此任务并继续生成下一条吗？</p><small>失败日志会保留，不会删除项目、素材或其他成片。</small></ConfirmDialog>}
    {dialog?.type==="cleanup"&&<CleanupDialog request={request} notify={notify} onClose={()=>setDialog(null)} onDone={refresh}/>} 
    {dialog?.type==="invalid"&&<InvalidDialog audit={dialog.audit} request={request} notify={notify} onClose={()=>setDialog(null)} onDone={refresh}/>} 
    <TaskLogDialog data={log} onClose={()=>setLog(null)}/>
  </div>;
}
