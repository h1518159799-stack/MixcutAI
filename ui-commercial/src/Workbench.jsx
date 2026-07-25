import React,{useEffect,useMemo,useState} from "react";

async function api(path,options={}){
  const response=await fetch(path,{headers:{"Content-Type":"application/json"},...options,body:options.body&&typeof options.body!=="string"?JSON.stringify(options.body):options.body});
  const json=await response.json();
  if(!json.ok)throw Error(json.error||"操作失败");
  return json.data;
}
const timeText=value=>{const n=Number(value||0);return `${Math.floor(n/60).toString().padStart(2,"0")}:${Math.floor(n%60).toString().padStart(2,"0")}`};
const todayText=()=>new Date().toLocaleDateString("sv-SE");
function dateText(value){const text=String(value||"").replace("T"," ");if(!text)return "—";const [date,time=""]=text.split(" ");return date.slice(5).replace("-","/")+(time?` ${time.slice(0,5)}`:"")}
function snapshotFromTasks(tasks=[]){
  const runningStates=["running","processing_shot","merging"];
  const active=tasks.filter(task=>runningStates.includes(task.status));
  const queued=tasks.filter(task=>task.status==="pending");
  return {running_count:active.length,queued_count:queued.length,today_completed_count:tasks.filter(task=>task.status==="completed"&&String(task.updated_at||task.created_at||"").slice(0,10)===todayText()).length,failed_count:tasks.filter(task=>task.status==="failed").length,current:active[0]||null,queued:queued.slice(0,2)};
}
function stateForProject(task,shots,matched){
  if(task&&["running","processing_shot","merging"].includes(task.status))return ["生成中","running"];
  if(task?.status==="pending")return ["等待生成","queued"];
  if(task?.status==="failed")return ["生成失败","failed"];
  if(task?.status==="completed")return ["已生成","completed"];
  if(!shots.length)return ["待添加镜头","draft"];
  if(matched<shots.length)return ["待匹配素材","draft"];
  return ["可生成","ready"];
}

export default function Workbench({db,setPage,refresh,notify,Button,Icon}){
  const [scanBusy,setScanBusy]=useState(false);
  const [playing,setPlaying]=useState(null);
  const [taskState,setTaskState]=useState(()=>snapshotFromTasks(db.tasks));
  useEffect(()=>setTaskState(snapshotFromTasks(db.tasks)),[db.tasks]);
  useEffect(()=>{
    let mounted=true;
    const update=async()=>{try{const next=await api("/api/dashboard/tasks");if(mounted)setTaskState(next)}catch(_error){}};
    const timer=window.setInterval(update,3000);
    return()=>{mounted=false;window.clearInterval(timer)};
  },[]);

  const scanMaterials=async()=>{
    try{
      setScanBusy(true);
      const picked=await api("/api/select-folder",{method:"POST",body:{}});
      if(!picked.path)return;
      const result=await api("/api/materials/scan",{method:"POST",body:{folder_path:picked.path}});
      notify(`扫描完成：新增 ${result.added}，更新 ${result.updated}`);
      await refresh(false);
    }finally{setScanBusy(false)}
  };
  const editTemplate=id=>{localStorage.setItem("active_template_id",String(id));setPage("镜头剪辑")};
  const projects=useMemo(()=>db.templates.slice(0,5).map(template=>{
    const shots=db.shots.filter(shot=>Number(shot.template_id)===Number(template.id));
    const matched=shots.filter(shot=>shot.matched_material_id).length;
    const latestTask=db.tasks.find(task=>Number(task.template_id)===Number(template.id)||task.template_name===template.name);
    return {template,shots,matched,state:stateForProject(latestTask,shots,matched)};
  }),[db.templates,db.shots,db.tasks]);
  const recentVideos=useMemo(()=>db.tasks.filter(task=>task.status==="completed"&&task.output_url).slice(0,4),[db.tasks]);
  const shortages=useMemo(()=>[...new Set(db.shots.map(shot=>Number(shot.material_category_id||0)).filter(Boolean))].map(categoryId=>{
    const category=db.categories.find(item=>Number(item.id)===categoryId);
    const categoryShots=db.shots.filter(shot=>Number(shot.material_category_id)===categoryId);
    const required=Math.max(0,...categoryShots.map(shot=>Number(shot.voice_duration||0)));
    const available=db.materials.filter(item=>Number(item.category_id)===categoryId&&Number(item.duration||0)>=required);
    return available.length?null:{id:categoryId,name:category?.name||"未命名分类",required};
  }).filter(Boolean).slice(0,3),[db.shots,db.categories,db.materials]);
  const todayGenerated=db.tasks.filter(task=>task.status==="completed"&&String(task.updated_at||task.created_at||"").slice(0,10)===todayText()).length;
  const current=taskState.current;
  const quickActions=[
    {icon:"plus",title:"开始新创作",desc:"进入镜头剪辑器",action:()=>setPage("镜头剪辑")},
    {icon:"grid",title:"批量生成",desc:"创建串行生产队列",action:()=>setPage("批量生成")},
    {icon:"folder",title:"扫描素材",desc:scanBusy?"正在读取本地文件":"更新媒体资产库",action:scanMaterials,disabled:scanBusy},
    {icon:"tasks",title:"查看任务队列",desc:`${taskState.queued_count} 个任务等待处理`,action:()=>setPage("任务中心")}
  ];

  return <div className="page dashboard workbench-page">
    <header className="workbench-heading"><div><h1>工作台</h1><p>本地 AI 视频生产概览</p></div><div><Button quiet icon="folder" disabled={scanBusy} onClick={scanMaterials}>{scanBusy?"正在扫描":"扫描素材"}</Button><Button primary icon="plus" onClick={()=>setPage("镜头剪辑")}>开始新创作</Button></div></header>

    <section className="workbench-section"><div className="workbench-section-title"><span>QUICK START</span><h2>快速开始</h2></div><div className="workbench-quick-grid">{quickActions.map(item=><button key={item.title} disabled={item.disabled} onClick={item.action}><i><Icon name={item.icon} size={19}/></i><span><b>{item.title}</b><small>{item.desc}</small></span><em>→</em></button>)}</div></section>

    <div className="workbench-primary-grid">
      <section className="workbench-card workbench-projects">
        <div className="workbench-card-head"><div><span>RECENT PROJECTS</span><h2>最近项目</h2></div><small>最近修改的 5 个模板</small></div>
        <div className="workbench-project-head"><span>创作项目</span><span>镜头</span><span>素材匹配</span><span>修改时间</span><span>状态</span><span></span></div>
        {projects.length?projects.map(({template,shots,matched,state})=><article key={template.id}><div className="workbench-project-name"><i>{String(template.name||"模").slice(0,1)}</i><span><b>{template.name}</b><small>{template.description||"本地创作模板"}</small></span></div><span>{shots.length} 个</span><span className={shots.length&&matched===shots.length?"match-ready":""}>{shots.length?`${matched}/${shots.length} 已匹配`:"未配置"}</span><span>{dateText(template.updated_at||template.created_at)}</span><span><em className={`workbench-status ${state[1]}`}>{state[0]}</em></span><button onClick={()=>editTemplate(template.id)}>继续编辑</button></article>):<div className="workbench-empty"><Icon name="layers" size={22}/><b>还没有创作项目</b><button onClick={()=>setPage("镜头剪辑")}>创建第一个模板</button></div>}
      </section>

      <section className="workbench-card workbench-task-status">
        <div className="workbench-card-head"><div><span>PRODUCTION STATUS</span><h2>任务状态</h2></div><button onClick={()=>setPage("任务中心")}>全部任务</button></div>
        <div className="workbench-task-metrics"><div><strong>{taskState.running_count}</strong><span>正在生成</span></div><div><strong>{taskState.queued_count}</strong><span>排队中</span></div><div><strong>{taskState.today_completed_count}</strong><span>今日完成</span></div><div className={taskState.failed_count?"has-error":""}><strong>{taskState.failed_count}</strong><span>失败</span></div></div>
        {current?<div className="workbench-current-task"><div><span>当前任务</span><b>{current.task_name||current.template_name}</b><strong>{Math.round(Number(current.progress||0))}%</strong></div><div className="workbench-progress"><i style={{width:`${Math.max(0,Math.min(100,Number(current.progress||0)))}%`}}/></div><p><Icon name="film" size={14}/> 当前处理：{current.current_shot||"准备生成"}</p></div>:<div className="workbench-current-empty"><Icon name="check" size={19}/><span><b>当前没有生成中的任务</b><small>新任务会在这里显示实时进度</small></span></div>}
        <div className="workbench-next-tasks"><b>后续队列</b>{taskState.queued.length?taskState.queued.map((task,index)=><div key={task.id}><i>{index+1}</i><span><b>{task.task_name||task.template_name}</b><small>{task.total_shots||0} 个镜头 · 等待处理</small></span></div>):<p>暂无排队任务</p>}</div>
      </section>
    </div>

    <div className="workbench-secondary-grid">
      <section className="workbench-card workbench-outputs"><div className="workbench-card-head"><div><span>RECENT OUTPUTS</span><h2>最近生成</h2></div><button onClick={()=>setPage("任务中心")}>查看全部</button></div><div className="workbench-output-grid">{recentVideos.length?recentVideos.map(task=><article key={task.id}><button className="workbench-output-thumb" onClick={()=>setPlaying(task)} aria-label={`播放 ${task.task_name||"成片"}`}><img loading="lazy" decoding="async" src={`/api/dashboard/tasks/${task.id}/thumbnail`} alt=""/><i><Icon name="play" size={18}/></i></button><div><b title={task.task_name||task.template_name}>{task.task_name||task.template_name||"未命名成片"}</b><small>{timeText(task.output_duration)} · {dateText(task.updated_at||task.created_at)}</small></div><footer><button onClick={()=>setPlaying(task)}><Icon name="play" size={13}/>播放</button><button onClick={()=>api(`/api/dashboard/tasks/${task.id}/open-output`,{method:"POST",body:{}}).catch(error=>notify(error.message))}><Icon name="folder" size={13}/>打开目录</button></footer></article>):<div className="workbench-output-empty"><Icon name="film" size={24}/><span><b>暂无已完成视频</b><small>完成生成后，成片会显示在这里</small></span></div>}</div></section>

      <section className="workbench-card workbench-resources"><div className="workbench-card-head"><div><span>RESOURCES</span><h2>资源概览</h2></div></div><div className="workbench-resource-grid"><div><span>素材总数</span><strong>{db.materials.length}</strong><small>个视频</small></div><div><span>素材分类</span><strong>{db.categories.length}</strong><small>个分类</small></div><div><span>创作模板</span><strong>{db.templates.length}</strong><small>个模板</small></div><div><span>BGM</span><strong>{db.bgms.length}</strong><small>首音乐</small></div><div><span>今日生成</span><strong>{todayGenerated}</strong><small>条成片</small></div></div><div className={`workbench-shortage ${shortages.length?"warning":"healthy"}`}><Icon name={shortages.length?"bell":"check"} size={17}/><div><b>{shortages.length?"素材不足提醒":"素材状态正常"}</b>{shortages.length?shortages.map(item=><small key={item.id}>{item.name}：缺少可覆盖 {item.required.toFixed(1)} 秒的素材</small>):<small>当前镜头分类均有可用素材</small>}</div>{shortages.length?<button onClick={()=>setPage("素材中心")}>去补充</button>:null}</div></section>
    </div>

    {playing?<div className="workbench-video-mask" role="dialog" aria-modal="true" aria-label="成片播放"><div className="workbench-video-modal"><header><div><b>{playing.task_name||playing.template_name||"成片预览"}</b><small>{dateText(playing.updated_at||playing.created_at)}</small></div><button onClick={()=>setPlaying(null)} aria-label="关闭">×</button></header><video controls preload="metadata" src={playing.output_url}/></div></div>:null}
  </div>;
}
