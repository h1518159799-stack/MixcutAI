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
  const enqueue=async id=>{try{await request(`/api/publish/tasks/${id}/enqueue`,{method:"POST",body:{}});notify("任务已加入队列");refresh(false)}catch(error){notify(error.message)}};
  const remove=async id=>{if(!confirm("确定删除该发布任务？"))return;await request(`/api/publish/tasks/${id}`,{method:"DELETE"});refresh(false)};
  const syncOutput=async()=>{try{setSyncing(true);const result=await request("/api/publish/sync-output",{method:"POST",body:{}});await refresh(false);notify(`已扫描输出目录 ${result.scanned_outputs} 个视频，清理 ${result.removed_publish_tasks} 个发布记录`)}catch(error){notify(error.message)}finally{setSyncing(false)}};
  return <div className="page schedule-page"><PageHeader Button={Button} db={db} onAccounts={onAccounts} onTests={onTests} syncing={syncing} onSync={syncOutput}/><div className="schedule-shell"><main><div className="schedule-guide">批量生成成功的成片会自动出现在本页。选择账号或分组后加入排期。</div><section className="schedule-stats"><article><strong>{drafts.length}</strong><span>待排期视频</span></article><article><strong>{(db.publish_tasks||[]).filter(task=>QUEUE_STATUSES.has(task.status)).length}</strong><span>排期中</span></article><article><strong>{incompleteMetadataCount}</strong><span>待补标题 / 话题</span></article><article><strong>{(db.publish_tasks||[]).filter(task=>["success","succeeded"].includes(task.status)).length}</strong><span>已有发布结果</span></article></section><div className="schedule-toolbar"><nav><button className={filter==="draft"?"active":""} onClick={()=>setFilter("draft")}>未排期</button><button className={filter==="queue"?"active":""} onClick={()=>setFilter("queue")}>排期中</button><button className={filter==="result"?"active":""} onClick={()=>setFilter("result")}>已有结果</button></nav><div><b>发布方式</b><button className={autoMode?"active":""} onClick={()=>setAutoMode(true)}>自动发布</button><button className={!autoMode?"active":""} onClick={()=>setAutoMode(false)}>手动确认</button></div></div>{filter!=="draft"&&<div className="publish-list-filter"><label><span>平台筛选</span><PlatformSelect platforms={platforms} value={platformFilter} includeAll compact onChange={setPlatformFilter}/></label><span>{visible.length} 条发布任务</span></div>}<h2 className="schedule-list-title">排期列表</h2><section className="schedule-list">{filter==="draft"?drafts.map(video=><PublishDraftCard key={video.id} video={video} accounts={accounts} groups={db.publish_account_groups||[]} platforms={platforms} profiles={db.profiles||[]} autoMode={autoMode} refresh={refresh} notify={notify} onPreview={setPreview} onOpenAiSettings={onOpenAiSettings} onAccounts={onAccounts} request={request} Button={Button} Icon={Icon}/>):visible.map(task=><article className="scheduled-card" key={task.id} onClick={()=>setPreview({url:task.video_url,title:task.title,path:task.video_path})}><span className={`publish-task-status ${task.status}`}>{STATUS_TEXT[task.status]}</span><div><b>{task.title}</b><small>{task.video_path.split(/[\\/]/).pop()} · {(task.channels||[]).map(item=>item.platform_name).join(" / ")}</small></div><em>{task.scheduled_at||"立即发布"}</em><div>{["pending","failed"].includes(task.status)&&<Button quiet onClick={event=>{event.stopPropagation();enqueue(task.id)}}>加入队列</Button>}<button onClick={event=>{event.stopPropagation();remove(task.id)}}>删除</button></div></article>)}{(filter==="draft"?drafts:visible).length===0&&<div className="schedule-empty">当前没有对应视频</div>}</section></main><aside className="schedule-preview"><h3>通用预览</h3><div>{preview?.url?<video src={preview.url} controls preload="metadata"/>:<span>点击“预览视频”<br/>在此处播放成片</span>}</div><b>{preview?.title||"尚未选择视频"}</b></aside></div></div>;
}

export default function PublishCenter({db,refresh,notify,request,Button,Icon,onOpenAiSettings}) {
  const [view,setView]=useState("tasks");
  if(view==="tests")return <PlatformTestPage db={db} refresh={refresh} notify={notify} request={request} Button={Button} onBack={()=>setView("tasks")}/>;
  if(view==="accounts")return <PublishAccountsPage db={db} refresh={refresh} notify={notify} onTasks={()=>setView("tasks")} onTests={()=>setView("tests")} Button={Button} Icon={Icon}/>;
  return <PublishTasks db={db} refresh={refresh} notify={notify} onAccounts={()=>setView("accounts")} onTests={()=>setView("tests")} onOpenAiSettings={onOpenAiSettings} request={request} Button={Button} Icon={Icon}/>;
}
