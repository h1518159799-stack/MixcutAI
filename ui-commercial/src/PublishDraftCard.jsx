import React, {useEffect, useMemo, useRef, useState} from "react";
import PlatformSelect from "./PlatformSelect.jsx";

const AI_STATUS_TEXT={
  not_started:"AI信息未生成",
  generating:"AI信息生成中",
  completed:"AI信息已生成",
  failed:"AI信息生成失败",
  manually_edited:"内容已手动修改",
};

function normalizeHashtags(value){
  const seen=new Set();
  return String(value||"").split(/[\s,，]+/).map(item=>item.replace(/^#+/,"").trim()).filter(item=>{
    const key=item.toLocaleLowerCase();
    if(!item||seen.has(key))return false;
    seen.add(key);return true;
  });
}

function hashtagText(value){
  return normalizeHashtags(Array.isArray(value)?value.join(" "):value).map(item=>`#${item}`).join(" ");
}

function accountGate(account){
  const capability=String(account.capability_status||"untested");
  if(capability==="untested")return {allowed:false,reason:"平台尚未完成验收"};
  if(capability==="failed")return {allowed:false,reason:"平台验收失败"};
  if(capability==="limited")return {allowed:false,reason:"平台当前仅部分可用"};
  if(capability!=="passed")return {allowed:false,reason:"平台尚未完成验收"};
  if(account.integration_type==="api"&&account.authorization_status!=="authorized")return {allowed:false,reason:"需要重新授权"};
  if(account.integration_type!=="api"&&account.login_status!=="logged_in")return {allowed:false,reason:"需要重新登录"};
  return {allowed:true,reason:"可发布"};
}

function shouldOpenAISettings(code){
  return ["AI_TEXT_SERVICE_NOT_CONFIGURED","AI_INVALID_KEY","AI_ENDPOINT_UNREACHABLE","AI_MODEL_UNAVAILABLE"].includes(code);
}

export default function PublishDraftCard({
  video,accounts,groups,platforms,profiles,autoMode,refresh,notify,onPreview,
  onOpenAiSettings,onAccounts,request,Button,Icon,
}){
  const metadata=video.publish_metadata||{};
  const initialPlatform=platforms.find(platform=>accounts.some(account=>Number(account.platform_id)===Number(platform.id)))?.id||0;
  const [form,setForm]=useState({
    generation_task_id:video.id,
    title:metadata.title||video.task_name||"",
    description:metadata.description||"",
    tags:metadata.hashtags||"",
    cover_path:"",
    cover_text:metadata.cover_text||"",
    scheduled_at:"",
    delete_after_publish:false,
    account_ids:[],
    ai_title:metadata.title||"",
    ai_description:metadata.description||"",
    ai_hashtags:normalizeHashtags(metadata.hashtags||""),
    ai_generated_time:metadata.ai_generated_at||"",
  });
  const [targetMode,setTargetMode]=useState("accounts");
  const [selectedPlatformId,setSelectedPlatformId]=useState(initialPlatform);
  const [selectedGroupId,setSelectedGroupId]=useState(0);
  const [groupExpanded,setGroupExpanded]=useState(false);
  const [groupPreview,setGroupPreview]=useState(null);
  const [scheduleMode,setScheduleMode]=useState("now");
  const [aiBusy,setAiBusy]=useState("");
  const [busy,setBusy]=useState(false);
  const [aiReview,setAiReview]=useState(null);
  const [aiError,setAiError]=useState(null);
  const dirtyFields=useRef(new Set());

  useEffect(()=>{
    setForm(current=>({
      ...current,
      title:dirtyFields.current.has("title")?current.title:(metadata.title||current.title),
      tags:dirtyFields.current.has("hashtags")?current.tags:(metadata.hashtags||current.tags),
      cover_text:dirtyFields.current.has("cover_text")?current.cover_text:(metadata.cover_text||current.cover_text),
      description:metadata.description||current.description,
      ai_generated_time:metadata.ai_generated_at||current.ai_generated_time,
    }));
  },[metadata.updated_at,metadata.ai_status]);

  const platformAccounts=useMemo(
    ()=>accounts.filter(account=>Number(account.platform_id)===Number(selectedPlatformId)),
    [accounts,selectedPlatformId],
  );
  const selectedGroup=groups.find(group=>Number(group.id)===Number(selectedGroupId));
  const groupMembers=selectedGroup?.members||[];
  const platformCode=platforms.find(item=>Number(item.id)===Number(selectedPlatformId))?.code||"douyin";

  const persistField=async(field,value,state="manually_edited")=>{
    try{
      await request(`/api/publish/drafts/${video.id}/metadata`,{method:"POST",body:{field,value,state}});
      dirtyFields.current.delete(field);
    }catch(error){notify(error.message)}
  };
  const editField=(field,formField,value)=>{
    dirtyFields.current.add(field);
    setForm(current=>({...current,[formField]:value}));
  };
  const choosePlatform=value=>{
    setSelectedPlatformId(Number(value));
    setForm(current=>({...current,account_ids:[]}));
  };
  const chooseAccount=value=>setForm(current=>({...current,account_ids:value?[Number(value)]:[]}));
  const changeTargetMode=value=>{
    setTargetMode(value);
    setGroupPreview(null);setGroupExpanded(false);
    if(value==="accounts")setSelectedGroupId(0);
    else setForm(current=>({...current,account_ids:[]}));
  };

  const regenerate=async mode=>{
    if(aiBusy)return;
    try{
      setAiBusy(mode);setAiError(null);
      const result=await request("/api/publish/ai-content",{method:"POST",body:{
        video_id:video.id,platform:platformCode,profile_id:profiles[0]?.id||0,
        current_title:form.title,current_tags:form.tags,current_cover_text:form.cover_text,
      }});
      const current=mode==="title"?form.title:mode==="hashtags"?form.tags:form.cover_text;
      const suggestion=mode==="title"
        ?(result.title||"")
        :mode==="hashtags"
          ?hashtagText([...normalizeHashtags(form.tags),...(result.hashtags||[])])
          :(result.cover_text||"");
      if(!suggestion)throw new Error("AI没有返回可用内容，请重新生成");
      setAiReview({mode,current,suggestion,result});
    }catch(error){
      setAiError({message:error.message||"AI内容生成失败，请稍后重试。",code:error.code||"AI_REQUEST_FAILED",logId:error.logId||""});
    }finally{setAiBusy("")}
  };
  const applyReview=async()=>{
    if(!aiReview)return;
    const mapping={title:"title",hashtags:"tags",cover_text:"cover_text"};
    const formField=mapping[aiReview.mode];
    setForm(current=>({...current,[formField]:aiReview.suggestion,
      ai_title:aiReview.mode==="title"?aiReview.suggestion:current.ai_title,
      ai_hashtags:aiReview.mode==="hashtags"?normalizeHashtags(aiReview.suggestion):current.ai_hashtags,
      ai_generated_time:aiReview.result?.ai_generated_time||current.ai_generated_time,
    }));
    await persistField(aiReview.mode,aiReview.suggestion,"completed");
    setAiReview(null);notify("已应用AI建议");
  };
  const chooseCover=async()=>{
    const result=await request("/api/select-cover",{method:"POST",body:{}});
    if(result.path)setForm(value=>({...value,cover_path:result.path}));
  };
  const remove=async()=>{
    if(!confirm(video.output_exists?"确定删除这条成片记录？原视频文件不会删除。":"成片文件已不存在，确定清理这条无效记录？"))return;
    await request(`/api/tasks/${video.id}`,{method:"DELETE"});notify("成片记录已删除");refresh(false);
  };
  const submit=async()=>{
    if(!video.output_exists)return notify("成片文件不存在，请删除记录后重新生成");
    if(scheduleMode==="timed"&&!form.scheduled_at)return notify("请选择定时发布时间");
    if(targetMode==="accounts"&&!form.account_ids.length)return notify("请选择发布账号");
    if(targetMode==="group"&&!selectedGroupId)return notify("请选择发布分组");
    try{
      setBusy(true);
      if(targetMode==="group"){
        const preview=await request("/api/publish/group-batches/preflight",{method:"POST",body:{...form,group_id:selectedGroupId}});
        setGroupPreview(preview);
      }else{
        const result=await request("/api/publish/tasks",{method:"POST",body:form});
        if(autoMode)await request(`/api/publish/tasks/${result.id}/enqueue`,{method:"POST",body:{}});
        notify(autoMode?"账号发布任务已加入队列":"账号待确认任务已创建");await refresh(false);
      }
    }catch(error){notify(error.message)}finally{setBusy(false)}
  };
  const confirmGroupPublish=async()=>{
    try{
      setBusy(true);
      const result=await request("/api/publish/group-batches",{method:"POST",body:{...form,group_id:selectedGroupId}});
      if(autoMode&&!result.duplicate){
        for(const taskId of result.task_ids||[])await request(`/api/publish/tasks/${taskId}/enqueue`,{method:"POST",body:{}});
      }
      notify(result.duplicate?"相同分组任务已存在，没有重复创建":`已创建 ${result.task_ids?.length||0} 个发布任务，跳过 ${result.blocked?.length||0} 个账号`);
      setGroupPreview(null);await refresh(false);
    }catch(error){notify(error.message)}finally{setBusy(false)}
  };

  const aiStatus=metadata.ai_status||"not_started";
  return <article className={`schedule-card ${!video.output_exists?"missing-output":""}`}>
    <div className="schedule-line"><i/><span>待排期</span><b>{video.created_at?.slice(5,16)} · {video.task_name}</b><em>{form.title&&form.tags?"标题 / 话题已填":"待补标题 / 话题"}</em></div>
    <div className="publish-ai-status-row">
      <span className={`publish-ai-state ${aiStatus}`}>{AI_STATUS_TEXT[aiStatus]||AI_STATUS_TEXT.not_started}</span>
      {aiStatus==="failed"&&<small>发布信息自动生成失败，你仍然可以手动填写或重新生成。</small>}
    </div>
    <div className="schedule-editor">
      <label>发布标题<textarea value={form.title} onChange={event=>editField("title","title",event.target.value)} onBlur={()=>persistField("title",form.title)}/></label>
      <label>发布话题<input value={form.tags} onChange={event=>editField("hashtags","tags",event.target.value)} onBlur={()=>persistField("hashtags",form.tags)} placeholder="#行业 #产品 #工厂"/></label>
      <label>发布时间<div className="schedule-time-mode"><button type="button" className={scheduleMode==="now"?"active":""} onClick={()=>{setScheduleMode("now");setForm(current=>({...current,scheduled_at:""}))}}>立即发布</button><button type="button" className={scheduleMode==="timed"?"active":""} onClick={()=>setScheduleMode("timed")}>定时发布</button></div>{scheduleMode==="timed"&&<input type="datetime-local" value={form.scheduled_at} onChange={event=>setForm({...form,scheduled_at:event.target.value})}/>}</label>
      <label className="cover-text-field"><span>封面文字 <button type="button" disabled={!!aiBusy} onClick={()=>regenerate("cover_text")}>{aiBusy==="cover_text"?"正在生成":"重新生成封面文字"}</button></span><input value={form.cover_text} onChange={event=>editField("cover_text","cover_text",event.target.value)} onBlur={()=>persistField("cover_text",form.cover_text)} placeholder="3-8字"/></label>
      <button className="schedule-cover" onClick={chooseCover}>{form.cover_path?<><b>已选择封面</b><small>{form.cover_path.split(/[\\/]/).pop()}</small></>:<><Icon name="plus" size={18}/><span>点击选择本地封面</span></>}</button>
      <section className="publish-ai-tools"><b>AI辅助</b><Button quiet disabled={!!aiBusy} onClick={()=>regenerate("title")}>{aiBusy==="title"?"正在生成":"重新生成标题"}</Button><Button quiet disabled={!!aiBusy} onClick={()=>regenerate("hashtags")}>{aiBusy==="hashtags"?"正在生成":"重新生成话题"}</Button></section>
      <section className="publish-target-control">
        <div className="publish-target-fields">
          <label><span>发布目标</span><select value={targetMode} onChange={event=>changeTargetMode(event.target.value)}><option value="accounts">单个账号</option><option value="group">账号分组</option></select></label>
          {targetMode==="accounts"&&<>
            <label><span>平台</span><PlatformSelect platforms={platforms.filter(platform=>accounts.some(account=>Number(account.platform_id)===Number(platform.id)))} value={selectedPlatformId} compact onChange={choosePlatform}/></label>
            <label><span>账号</span><select className="publish-account-select" value={form.account_ids[0]||""} onChange={event=>chooseAccount(event.target.value)}><option value="">选择该平台账号</option>{platformAccounts.map(account=>{const gate=accountGate(account);return <option key={account.id} value={account.id} disabled={!gate.allowed}>{account.account_name}{account.channel_title?` · ${account.channel_title}`:""} · {gate.reason}</option>})}</select></label>
          </>}
          {targetMode==="group"&&groups.length>0&&<label><span>账号分组</span><select className="publish-group-select" value={selectedGroupId} onChange={event=>{setSelectedGroupId(Number(event.target.value));setGroupExpanded(false)}}><option value={0}>选择发布分组</option>{groups.map(group=><option key={group.id} value={group.id}>{group.name} · {group.account_count}个账号 · {group.publishable_count}个可发布</option>)}</select></label>}
        </div>
        {targetMode==="group"&&groups.length===0&&<div className="publish-group-empty"><b>还没有账号分组</b><span>请先将多个平台账号组合成发布矩阵。</span><Button quiet onClick={onAccounts}>新建账号分组</Button></div>}
        {targetMode==="group"&&selectedGroup&&<div className="publish-group-summary"><button type="button" onClick={()=>setGroupExpanded(value=>!value)}><span>账号总数 {selectedGroup.account_count||groupMembers.length} · 可发布 {selectedGroup.publishable_count||0} · 需要登录 {selectedGroup.needs_login_count||0} · 待验收 {selectedGroup.untested_count||0}</span><b>{groupExpanded?"收起组内账号":"查看组内账号"}</b></button>{groupExpanded&&<div className="publish-group-members">{groupMembers.map(member=><span key={member.id||member.account_id}><b>{member.platform_name||"平台"} · {member.account_name||"账号"}</b><em>{member.publishable?"可发布":member.blocked_reason||"待处理"}</em></span>)}</div>}</div>}
        <div className="publish-target-buttons"><Button quiet onClick={()=>onPreview({url:video.output_url,title:form.title,path:video.output_path})}>预览视频</Button><Button quiet className="draft-delete" onClick={remove}>{video.output_exists?"删除记录":"删除无效记录"}</Button><Button primary disabled={busy} onClick={submit}>{busy?"处理中":targetMode==="group"?"加入分组发布队列":"加入账号发布队列"}</Button></div>
      </section>
      <label className="schedule-delete"><input type="checkbox" checked={form.delete_after_publish} onChange={event=>setForm({...form,delete_after_publish:event.target.checked})}/>全部成功后删除本地视频</label>
    </div>
    {aiReview&&<div className="publish-ai-modal"><div className="publish-ai-dialog"><header><div><small>AI内容建议</small><h3>{aiReview.mode==="title"?"确认发布标题":aiReview.mode==="hashtags"?"确认发布话题":"确认封面文字"}</h3></div><button type="button" onClick={()=>setAiReview(null)}>关闭</button></header><section className="publish-ai-compare"><div><b>{aiReview.mode==="title"?"发布标题":aiReview.mode==="hashtags"?"发布话题":"封面文字"}</b><p><span>当前内容</span>{aiReview.current||"未填写"}</p><i>→</i><p className="suggested"><span>AI建议</span>{aiReview.suggestion}</p></div></section><footer><Button quiet onClick={()=>setAiReview(null)}>取消</Button><Button primary onClick={applyReview}>应用建议</Button></footer></div></div>}
    {aiError&&<div className="publish-ai-modal"><div className="publish-ai-dialog publish-ai-error"><header><div><small>AI生成失败</small><h3>{aiError.message.split("\n")[0]}</h3></div><button type="button" onClick={()=>setAiError(null)}>关闭</button></header><p>{aiError.message.split("\n").slice(1).join(" ")||"AI内容生成失败，请稍后重试。"}</p>{aiError.logId&&<small>日志编号：{aiError.logId}</small>}<footer><Button quiet onClick={()=>setAiError(null)}>取消</Button>{shouldOpenAISettings(aiError.code)&&<Button primary onClick={()=>{setAiError(null);onOpenAiSettings?.()}}>去配置</Button>}</footer></div></div>}
    {groupPreview&&<div className="group-publish-confirm"><div className="group-publish-confirm-box"><header><div><small>分组发布确认</small><h3>{groupPreview.group?.name}</h3></div><button onClick={()=>setGroupPreview(null)}>关闭</button></header><div className="group-confirm-counts"><span>本次选择<b>{groupPreview.total_count}</b>个账号</span><span>可发布<b>{groupPreview.publishable_count}</b>个</span><span>暂不可发布<b>{groupPreview.blocked_count}</b>个</span></div><section><h4>本次可发布</h4>{groupPreview.publishable.map(item=><p key={item.account_id} className="allowed"><b>{item.platform_name} · {item.account_name}</b><span>将创建独立发布任务</span></p>)}</section><section><h4>暂时不能发布</h4>{groupPreview.blocked.map(item=><p key={item.account_id} className="blocked"><b>{item.platform_name} · {item.account_name}</b><span>{item.blocked_reason}</span></p>)}</section><footer><Button quiet onClick={()=>setGroupPreview(null)}>取消</Button><Button primary disabled={busy} onClick={confirmGroupPublish}>仅发布可用账号</Button></footer></div></div>}
  </article>;
}
