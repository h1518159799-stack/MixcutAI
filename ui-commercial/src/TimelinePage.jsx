import React,{useMemo,useState} from "react";

const L={
  title:"AI 视频生产检查",
  desc:"生成前检查每个镜头的画面、配音、字幕和衔接状态。",
  scan:"扫描音效",
  none:"未生成",
  second:"秒",
  done:"音效扫描完成",
};

const effects=[
  ["fade","淡入淡出"],
  ["slide_left","左滑"],
  ["slide_right","右滑"],
  ["zoom","缩放"],
  ["flash","闪白"],
  ["blur","模糊"],
];
const effectName=value=>effects.find(item=>item[0]===value)?.[1]||"淡入淡出";
const fmt=value=>{const n=Number(value||0);return `${String(Math.floor(n/60)).padStart(2,"0")}:${String(Math.floor(n%60)).padStart(2,"0")}`};
const fileName=value=>String(value||"").split(/[\\/]/).pop();

export default function TimelinePage({db,refresh,notify,Button,Icon,request}){
  const[templateId,setTemplateId]=useState(db.templates[0]?.id||0);
  const[advanced,setAdvanced]=useState(false);
  const shots=useMemo(()=>db.shots.filter(item=>item.template_id===Number(templateId)),[db.shots,templateId]);
  const timeline=useMemo(()=>{let cursor=0;return shots.map((shot,index)=>{const previous=shots[index-1];const transition=previous?(db.transitions||[]).find(item=>item.from_shot_id===previous.id&&item.to_shot_id===shot.id):null;const overlap=transition?.enabled?Number(transition.duration||0):0;const start=Math.max(0,cursor-overlap);const end=start+Number(shot.voice_duration||0);cursor=end;return{shot,start,end,transition}})},[shots,db.transitions]);
  const total=timeline.at(-1)?.end||0;
  const maxTick=Math.max(5,Math.ceil(total/5)*5);
  const ticks=Array.from({length:Math.floor(maxTick/5)+1},(_,index)=>index*5);
  const matchedSfx=shot=>(db.sound_effects||[]).find(effect=>String(effect.keywords||"").replace(/[，；;]/g,",").split(",").some(key=>key.trim()&&String(shot.ai_text||shot.original_text||"").includes(key.trim())));
  const save=async(from,to,patch)=>{const current=(db.transitions||[]).find(item=>item.from_shot_id===from&&item.to_shot_id===to)||{effect:"fade",duration:.35,enabled:true};await request("/api/transitions",{method:"POST",body:{...current,...patch,from_shot_id:from,to_shot_id:to}});refresh(false)};
  const scan=async()=>{const folder=await request("/api/select-folder",{method:"POST",body:{}});if(folder.path){const result=await request("/api/sfx/scan",{method:"POST",body:{folder_path:folder.path}});notify(`${L.done}: ${result.total}`);refresh(false)}};

  return <div className="page timeline-studio timeline-check-page">
    <header className="timeline-head">
      <div><span>PRODUCTION CHECK</span><h1>{L.title}</h1><p>{L.desc}</p></div>
      <div className="timeline-head-actions">
        <select value={templateId} onChange={event=>setTemplateId(Number(event.target.value))}>{db.templates.map(template=><option key={template.id} value={template.id}>{template.name}</option>)}</select>
        <Button quiet onClick={()=>setAdvanced(value=>!value)}>{advanced?"收起转场与音效":"转场与音效"}</Button>
        <Button icon="folder" onClick={scan}>{L.scan}</Button>
      </div>
    </header>

    {!shots.length?<section className="timeline-check-empty"><Icon name="timeline" size={28}/><b>当前模板还没有镜头</b><span>请先在镜头剪辑中添加镜头，再回来检查时间轴。</span></section>:<>
      <section className="inspection-timeline">
        <div className="inspection-ruler"><span>时间</span><div>{ticks.map(tick=><i key={tick} style={{left:`${tick/maxTick*100}%`}}><b>{fmt(tick)}</b></i>)}</div></div>
        <div className="inspection-body">
          <aside className="inspection-labels"><div className="video-label"><Icon name="film" size={16}/><b>视频</b></div><div><Icon name="waveform" size={16}/><b>配音</b></div><div><Icon name="caption" size={16}/><b>字幕</b></div></aside>
          <main className="inspection-scroll"><div className="inspection-shots">{timeline.map(({shot,start,end,transition},index)=>{const duration=Math.max(0,end-start);const voiceReady=Boolean(shot.voice_file_path)&&Number(shot.voice_duration||0)>0;const subtitleReady=Boolean(shot.subtitle_file_path)&&Boolean(Number(shot.enable_subtitle??1));return <article className="inspection-shot" key={shot.id} style={{width:`${Math.max(178,Math.min(270,duration*31))}px`}}>
            <section className="inspection-video">
              <header><span>镜头 {index+1}</span><time>{fmt(start)}–{fmt(end)}</time></header>
              <div className="inspection-thumb">{shot.matched_thumbnail_url?<img src={shot.matched_thumbnail_url} alt="" loading="lazy"/>:<div><Icon name="film" size={21}/><span>未匹配素材</span></div>}<em>{duration.toFixed(1)}s</em></div>
              <footer><b title={shot.shot_name}>{shot.shot_name||`镜头 ${index+1}`}</b><small>{shot.matched_material_id?`${shot.category_name||"视频素材"} · 已匹配`:"等待匹配视频素材"}</small></footer>
              {index>0?<span className={`transition-state ${transition?.enabled?"ready":"off"}`}>{transition?.enabled?effectName(transition.effect):"转场关闭"}</span>:<span className="transition-state first">起始镜头</span>}
            </section>
            <section className={`inspection-voice ${voiceReady?"ready":"empty"}`}><i/><div><b>{voiceReady?"配音已生成":"未生成配音"}</b><small>{voiceReady?`${fileName(shot.voice_file_path)} · ${Number(shot.voice_duration).toFixed(1)} 秒`:"请先生成镜头语音"}</small></div></section>
            <section className={`inspection-subtitle ${subtitleReady?"ready":"empty"}`}><i/><div><b>{subtitleReady?"字幕已生成":"未生成字幕"}</b><small>{subtitleReady?"ASS 字幕已就绪":"该镜头暂无字幕文件"}</small></div></section>
          </article>})}</div></main>
        </div>
        <div className="inspection-bgm"><span><Icon name="music" size={16}/><b>BGM</b></span><div className={(db.bgms||[]).length?"ready":"empty"}><i/><b>{db.bgms[0]?.name||"未添加背景音乐"}</b><small>{(db.bgms||[]).length?`覆盖整条视频 · ${Number(db.bgms[0]?.duration||0).toFixed(1)} 秒`:"可在镜头剪辑页面选择 BGM"}</small></div></div>
      </section>

      {advanced?<section className="advanced-timeline">
        <header><div><span>ADVANCED</span><b>高级轨道</b></div><small>音效与转场仅在需要调整时展开</small></header>
        <div className="advanced-lane"><label>SFX</label><main>{timeline.map(({shot})=>{const sfx=matchedSfx(shot);return <div className={sfx?"ready":"empty"} key={shot.id}><b>{sfx?.name||"未添加音效"}</b><small>{sfx?sfx.category||"自动匹配":"当前文案没有匹配音效"}</small></div>})}</main></div>
        <div className="advanced-lane transition-lane"><label>转场</label><main>{timeline.slice(1).map(({shot,transition},index)=>{const from=timeline[index].shot.id;return <div className="transition-editor" key={shot.id}><button className={transition?.enabled?"on":""} onClick={()=>save(from,shot.id,{enabled:!transition?.enabled})}>{transition?.enabled?"开启":"关闭"}</button><select value={transition?.effect||"fade"} onChange={event=>save(from,shot.id,{effect:event.target.value,enabled:true})}>{effects.map(effect=><option key={effect[0]} value={effect[0]}>{effect[1]}</option>)}</select><input type="number" min="0.05" max="2" step="0.05" value={transition?.duration||.35} onChange={event=>save(from,shot.id,{duration:Number(event.target.value),enabled:true})}/><span>秒</span></div>})}</main></div>
      </section>:null}
    </>}
  </div>;
}
