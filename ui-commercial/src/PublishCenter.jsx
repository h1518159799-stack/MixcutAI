import React, {useEffect, useState} from "react";
import PlatformSelect, {PlatformMark, platformName} from "./PlatformSelect.jsx";
import PublishAccountsPage from "./PublishAccountsPage.jsx";
import PlatformTestPage from "./PlatformTestPage.jsx";
import PublishDraftCard from "./PublishDraftCard.jsx";

const STATUS_TEXT={pending:"待发布",draft:"草稿",prechecking:"预检中",ready:"准备发布",queued:"排队中",uploading:"上传中",submitting:"提交中",processing:"平台处理中",waiting_confirmation:"待确认",publishing:"发布中",succeeded:"发布成功",success:"成功",needs_login:"需要重新登录",needs_manual_action:"需要人工处理",cancelled:"已取消",failed:"失败"};
const QUEUE_STATUSES=new Set(["pending","ready","prechecking","queued","uploading","submitting","processing","waiting_confirmation","publishing","failed","needs_login","needs_manual_action"]);

function normalizeHashtags(value) {
  const seen=new Set();
  return String(value||"").split(/[\s,，]+/).map(item=>item.replace(/^#+/,"").trim()).filter(item=>{
    const key=item.toLocaleLowerCase();
    if(!item||seen.has(key))return false;
    seen.add(key);return true;
  });
}

function hashtagText(value) {
  return normalizeHashtags(Array.isArray(value)?value.join(" "):value).map(item=>`#${item}`).join(" ");
}

function shouldOpenAISettings(code) {
  return ["AI_TEXT_SERVICE_NOT_CONFIGURED","AI_INVALID_KEY","AI_ENDPOINT_UNREACHABLE","AI_MODEL_UNAVAILABLE"].includes(code);
}

function PageHeader({Button,db,onAccounts,onTests,syncing,onSync}) {
  return <header className="page-header"><div><span>发布中心</span><h1>发布排期</h1><p>为成片补充内容并选择单个账号或发布分组。</p></div><div><Button quiet icon="folder" disabled={syncing} onClick={onSync}>{syncing?"正在扫描":"同步输出目录"}</Button><Button quiet onClick={onAccounts}>账号管理</Button>{db.admin_mode&&<Button quiet onClick={onTests}>平台发布测试</Button>}</div></header>;
}

function PublishTasks({db,refresh,notify,onAccounts,onTests,onOpenAiSettings,request,Button,Icon}) {
  const supported=new Set(["douyin","tiktok","youtube","instagram","facebook","xiaohongshu","kuaishou","wechat_channels","bilibili"]);
  const accounts=(db.publish_accounts||[]).filter(account=>supported.has(account.platform_code));
  const platforms=(db.platforms||[]).filter(platform=>platform.enabled&&supported.has(platform.code));
  const scheduledIds=new Set((db.publish_tasks||[]).map(task=>task.generation_task_id).filter(Boolean));
  const drafts=(db.tasks||[]).filter(task=>["succeeded","completed"].includes(task.status)&&task.output_path&&task.output_exists&&!scheduledIds.has(task.id));
  const incompleteMetadataCount=drafts.filter(task=>{
    const metadata=task.publish_metadata||{};
    return !["completed","manually_edited"].includes(metadata.ai_status)||!String(metadata.title||"").trim()||!String(metadata.hashtags||"").trim();
  }).length;
  const [filter,setFilter]=useState("draft");
  const [platformFilter,setPlatformFilter]=useState("all");
  const [autoMode,setAutoMode]=useState(true);
  const [preview,setPreview]=useState(null);
  const [syncing,setSyncing]=useState(false);
  const [reasonTask,setReasonTask]=useState(null);
  const [busyTask,setBusyTask]=useState(0);
  const metadataGenerating=drafts.some(task=>task.publish_metadata?.ai_status==="generating"||(
    task.publish_metadata?.ai_status==="not_started"&&!task.publish_metadata?.legacy_record
  ));
  useEffect(()=>{
    if(!metadataGenerating)return undefined;
    const timer=window.setInterval(()=>refresh(false),1500);
    return()=>window.clearInterval(timer);
  },[metadataGenerating,refresh]);
  const baseVisible=filter==="queue"?(db.publish_tasks||[]).filter(task=>QUEUE_STATUSES.has(task.status)):(db.publish_tasks||[]).filter(task=>["success","succeeded"].includes(task.status));
  const visible=platformFilter==="all"?baseVisible:baseVisible.filter(task=>(task.channels||[]).some(channel=>Number(channel.platform_id)===Number(platformFilter)||channel.platform_code===platforms.find(item=>Number(item.id)===Number(platformFilter))?.code));
  const enqueue=async id=>{try{setBusyTask(id);await request(`/api/publish/tasks/${id}/enqueue`,{method:"POST",body:{}});const task=(db.publish_tasks||[]).find(item=>Number(item.id)===Number(id));const names=[...new Set((task?.channels||[]).map(item=>item.platform_name).filter(Boolean))].join("、")||"对应平台";notify(`正在核对${names}后台结果，确认未发布后才会重新上传`);await refresh(false)}catch(error){notify(error.message)}finally{setBusyTask(0)}};
  const remove=async id=>{if(!confirm("确定删除该发布任务？"))return;await request(`/api/publish/tasks/${id}`,{method:"DELETE"});refresh(false)};
  const syncOutput=async()=>{try{setSyncing(true);const result=await request("/api/publish/sync-output",{method:"POST",body:{}});await refresh(false);notify(`已扫描输出目录 ${result.scanned_outputs} 个视频，清理 ${result.removed_publish_tasks} 个发布记录`)}catch(error){notify(error.message)}finally{setSyncing(false)}};
  return <div className="page schedule-page"><PageHeader Button={Button} db={db} onAccounts={onAccounts} onTests={onTests} syncing={syncing} onSync={syncOutput}/><div className="schedule-shell"><main><div className="schedule-guide">批量生成成功的成片会自动出现在本页。选择账号或分组后加入排期。</div><section className="schedule-stats"><article><strong>{drafts.length}</strong><span>待排期视频</span></article><article><strong>{(db.publish_tasks||[]).filter(task=>QUEUE_STATUSES.has(task.status)).length}</strong><span>排期中</span></article><article><strong>{incompleteMetadataCount}</strong><span>待补标题 / 话题</span></article><article><strong>{(db.publish_tasks||[]).filter(task=>["success","succeeded"].includes(task.status)).length}</strong><span>已有发布结果</span></article></section><div className="schedule-toolbar"><nav><button className={filter==="draft"?"active":""} onClick={()=>setFilter("draft")}>未排期</button><button className={filter==="queue"?"active":""} onClick={()=>setFilter("queue")}>排期中</button><button className={filter==="result"?"active":""} onClick={()=>setFilter("result")}>已有结果</button></nav><div><b>发布方式</b><button className={autoMode?"active":""} onClick={()=>setAutoMode(true)}>自动发布</button><button className={!autoMode?"active":""} onClick={()=>setAutoMode(false)}>手动确认</button></div></div>{filter!=="draft"&&<div className="publish-list-filter"><label><span>平台筛选</span><PlatformSelect platforms={platforms} value={platformFilter} includeAll compact onChange={setPlatformFilter}/></label><span>{visible.length} 条发布任务</span></div>}<h2 className="schedule-list-title">排期列表</h2><section className="schedule-list">{filter==="draft"?drafts.map(video=><PublishDraftCard key={video.id} video={video} accounts={accounts} groups={db.publish_account_groups||[]} platforms={platforms} profiles={db.profiles||[]} autoMode={autoMode} refresh={refresh} notify={notify} onPreview={setPreview} onOpenAiSettings={onOpenAiSettings} onAccounts={onAccounts} request={request} Button={Button} Icon={Icon}/>):visible.map(task=>{
    const channel=(task.channels||[])[0]||{};
    const reason=channel.error_message||task.last_error||"发布过程未返回具体原因";
    const failed=["failed","needs_login","needs_manual_action"].includes(task.status);
    return <article className={`scheduled-card ${failed?"has-publish-error":""}`} key={task.id} onClick={()=>setPreview({url:task.video_url,title:task.title,path:task.video_path})}><span className={`publish-task-status ${task.status}`}>{STATUS_TEXT[task.status]}</span><div><b>{task.title}</b><small>{task.video_path.split(/[\\/]/).pop()} · {(task.channels||[]).map(item=>item.platform_name).join(" / ")}</small>{failed&&<p className="publish-failure-summary">{reason.split("（日志编号")[0]}</p>}</div><em>{task.scheduled_at||"立即发布"}</em><div>{task.status==="pending"&&<Button quiet onClick={event=>{event.stopPropagation();enqueue(task.id)}}>加入队列</Button>}{failed&&<Button quiet onClick={event=>{event.stopPropagation();setReasonTask(task)}}>查看原因</Button>}{task.status==="failed"&&<Button quiet disabled={busyTask===task.id} onClick={event=>{event.stopPropagation();enqueue(task.id)}}>{busyTask===task.id?"正在核对":"重新发布"}</Button>}{task.status==="needs_login"&&<Button quiet onClick={event=>{event.stopPropagation();onAccounts()}}>重新登录</Button>}{task.status==="needs_manual_action"&&<Button quiet onClick={event=>{event.stopPropagation();setReasonTask(task)}}>继续处理</Button>}<button onClick={event=>{event.stopPropagation();remove(task.id)}}>删除</button></div></article>;
  })}{(filter==="draft"?drafts:visible).length===0&&<div className="schedule-empty">当前没有对应视频</div>}</section></main><aside className="schedule-preview"><h3>通用预览</h3><div>{preview?.url?<video src={preview.url} controls preload="metadata"/>:<span>点击“预览视频”<br/>在此处播放成片</span>}</div><b>{preview?.title||"尚未选择视频"}</b></aside></div>{reasonTask&&<PublishReasonDialog task={reasonTask} onClose={()=>setReasonTask(null)} onRetry={()=>{setReasonTask(null);enqueue(reasonTask.id)}} onLogin={()=>{setReasonTask(null);onAccounts()}} Button={Button}/>}</div>;
}

function PublishReasonDialog({task,onClose,onRetry,onLogin,Button}) {
  const channel=(task.channels||[])[0]||{};
  let evidence={};
  try{evidence=JSON.parse(channel.result_evidence_json||"{}")||{}}catch{}
  const diagnosis=evidence.diagnosis||{};
  const raw=channel.error_message||task.last_error||diagnosis.message||"发布过程未返回具体原因";
  // Older publish rows predate structured error codes. Preserve their real
  // message and infer only the known evidence-confirmation failure so users
  // receive the same safe, duplicate-aware retry path as new rows.
  const legacyNeedsConfirmation=raw.includes("真实作品ID")||raw.includes("作品链接");
  const code=channel.error_code||task.error_code||diagnosis.error_code||(legacyNeedsConfirmation?"MANUAL_CONFIRM_REQUIRED":"UNKNOWN_ERROR");
  const message=raw.split("（日志编号")[0];
  const stage=diagnosis.stage||({prechecking:"发布前检查",uploading:"上传视频",submitting:"提交发布",processing:"平台处理",waiting_confirmation:"等待平台确认"}[channel.status])||"平台结果确认";
  const platform=channel.platform_name||task.platform_name||"未知平台";
  const advice=code==="AUTH_EXPIRED"?`请重新登录该${platform}账号后再重试。`:code==="FILE_NOT_FOUND"?"请重新生成视频或检查输出目录。":code==="PAGE_CHANGED"?`${platform}发布页面结构可能已更新，请更新对应发布适配器。`:code==="MANUAL_CONFIRM_REQUIRED"?`请先在${platform}后台核对作品，软件重试时也会先核对，避免重复发布。`:`确认${platform}账号和网络正常后重新发布。`;
  const copy=()=>navigator.clipboard?.writeText(`平台：${platform}\n账号：${channel.account_name||""}\n视频：${task.video_path?.split(/[\\/]/).pop()||""}\n失败阶段：${stage}\n原因：${message}\n错误码：${code}\n日志编号：${channel.technical_log_id||task.technical_log_id||diagnosis.technical_log_id||"无"}`);
  return <div className="publish-reason-mask"><section className="publish-reason-dialog"><header><div><small>发布诊断</small><h3>{platform}发布失败</h3></div><button onClick={onClose}>×</button></header><dl><div><dt>平台</dt><dd>{platform}</dd></div><div><dt>账号</dt><dd>{channel.account_name||"未记录"}</dd></div><div><dt>视频</dt><dd>{task.video_path?.split(/[\\/]/).pop()}</dd></div><div><dt>失败阶段</dt><dd>{stage}</dd></div><div><dt>原因</dt><dd>{message}</dd></div><div><dt>处理建议</dt><dd>{advice}</dd></div><div><dt>错误码</dt><dd><code>{code}</code></dd></div><div><dt>日志编号</dt><dd><code>{channel.technical_log_id||task.technical_log_id||diagnosis.technical_log_id||"未生成"}</code></dd></div></dl><footer><Button quiet onClick={copy}>复制诊断信息</Button>{code==="AUTH_EXPIRED"?<Button primary onClick={onLogin}>重新登录</Button>:<Button primary onClick={onRetry}>重新发布</Button>}<Button quiet onClick={onClose}>关闭</Button></footer></section></div>;
}

export default function PublishCenter({db,refresh,notify,request,Button,Icon,onOpenAiSettings}) {
  const [view,setView]=useState("tasks");
  if(view==="tests")return <PlatformTestPage db={db} refresh={refresh} notify={notify} request={request} Button={Button} onBack={()=>setView("tasks")}/>;
  if(view==="accounts")return <PublishAccountsPage db={db} refresh={refresh} notify={notify} onTasks={()=>setView("tasks")} onTests={()=>setView("tests")} Button={Button} Icon={Icon}/>;
  return <PublishTasks db={db} refresh={refresh} notify={notify} onAccounts={()=>setView("accounts")} onTests={()=>setView("tests")} onOpenAiSettings={onOpenAiSettings} request={request} Button={Button} Icon={Icon}/>;
}
