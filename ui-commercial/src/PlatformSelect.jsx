import React, {useEffect, useMemo, useRef, useState} from "react";

const PLATFORM_NAMES = {
  douyin: "抖音",
  xiaohongshu: "小红书",
  kuaishou: "快手",
  wechat_channels: "视频号",
  bilibili: "B站",
  tiktok: "TikTok",
  youtube: "YouTube",
  instagram: "Instagram Business",
  facebook: "Facebook Page",
  x: "X",
};

const CAPABILITY_TEXT = {
  passed: "已验证",
  untested: "待验收",
  testing: "测试中",
  failed: "测试失败",
  limited: "部分可用",
  not_implemented: "未实现",
};

export function platformName(platform) {
  return PLATFORM_NAMES[platform?.code] || platform?.name || "平台";
}

function groupName(platform) {
  if (platform?.region !== "overseas") return "国内平台";
  return platform?.integration_type === "api" ? "海外 API 平台" : "海外浏览器平台";
}

function accessText(platform) {
  const access = platform?.integration_type === "api" ? "官方 API" : "独立浏览器";
  const status = CAPABILITY_TEXT[platform?.capability_status] || "待配置";
  return `${access} · ${status}`;
}

export function PlatformMark({platform, size=18}) {
  const label = platformName(platform);
  const short = ({douyin:"抖",xiaohongshu:"红",kuaishou:"快",wechat_channels:"视",bilibili:"B",tiktok:"T",youtube:"Y",instagram:"I",facebook:"f",x:"X"})[platform?.code] || label.slice(0,1);
  return <span className={`platform-mark platform-${platform?.code || "default"}`} style={{width:size,height:size,fontSize:Math.max(9,size*.56)}} aria-hidden="true">{short}</span>;
}

export default function PlatformSelect({
  platforms=[],
  value,
  onChange,
  disabled=false,
  includeAll=false,
  allLabel="全部平台",
  placeholder="选择平台",
  className="",
  compact=false,
}) {
  const rootRef = useRef(null);
  const searchRef = useRef(null);
  const [open,setOpen] = useState(false);
  const [query,setQuery] = useState("");
  const [activeIndex,setActiveIndex] = useState(0);
  const selected = platforms.find(item=>String(item.id)===String(value) || item.code===value);
  const normalized = query.trim().toLowerCase();
  const filtered = useMemo(()=>platforms.filter(item=>{
    const haystack = `${platformName(item)} ${item.code || ""} ${accessText(item)}`.toLowerCase();
    return !normalized || haystack.includes(normalized);
  }),[platforms,normalized]);
  const options = includeAll && !normalized ? [{id:"all",code:"all",name:allLabel,_all:true},...filtered] : filtered;
  const grouped = useMemo(()=>{
    const result=[];
    options.forEach(item=>{
      const title=item._all?"":groupName(item);
      let group=result.find(entry=>entry.title===title);
      if(!group){group={title,items:[]};result.push(group)}
      group.items.push(item);
    });
    return result;
  },[options]);

  useEffect(()=>{
    const close=event=>{if(!rootRef.current?.contains(event.target))setOpen(false)};
    document.addEventListener("mousedown",close);
    return()=>document.removeEventListener("mousedown",close);
  },[]);
  useEffect(()=>{if(open){setQuery("");setActiveIndex(0);requestAnimationFrame(()=>searchRef.current?.focus())}},[open]);

  const choose=item=>{
    onChange?.(item._all ? "all" : item.id, item._all ? null : item);
    setOpen(false);
  };
  const onKeyDown=event=>{
    if(!open && ["Enter"," ","ArrowDown"].includes(event.key)){event.preventDefault();setOpen(true);return}
    if(!open)return;
    if(event.key==="Escape"){event.preventDefault();setOpen(false)}
    if(event.key==="ArrowDown"){event.preventDefault();setActiveIndex(index=>Math.min(options.length-1,index+1))}
    if(event.key==="ArrowUp"){event.preventDefault();setActiveIndex(index=>Math.max(0,index-1))}
    if(event.key==="Enter" && options[activeIndex]){event.preventDefault();choose(options[activeIndex])}
  };
  const displayAll=includeAll && (value==="all" || value==="" || value==null);
  return <div ref={rootRef} className={`platform-select ${compact?"compact":""} ${open?"open":""} ${disabled?"disabled":""} ${className}`} onKeyDown={onKeyDown}>
    <button type="button" className="platform-select-trigger" disabled={disabled} aria-haspopup="listbox" aria-expanded={open} onClick={()=>setOpen(state=>!state)}>
      {selected?<PlatformMark platform={selected}/>:<span className="platform-mark platform-all">{displayAll?"全":"·"}</span>}
      <span className="platform-select-value"><b>{selected?platformName(selected):(displayAll?allLabel:placeholder)}</b>{selected&&!compact&&<small>{accessText(selected)}</small>}</span>
      <svg viewBox="0 0 20 20" aria-hidden="true"><path d="m6 8 4 4 4-4"/></svg>
    </button>
    {open&&<div className="platform-select-popover">
      <label className="platform-select-search"><svg viewBox="0 0 20 20"><circle cx="9" cy="9" r="5.5"/><path d="m13 13 4 4"/></svg><input ref={searchRef} value={query} onChange={event=>{setQuery(event.target.value);setActiveIndex(0)}} placeholder="搜索平台"/></label>
      <div className="platform-select-options" role="listbox">
        {grouped.map(group=><section key={group.title||"all"}>{group.title&&<h4>{group.title}</h4>}{group.items.map(item=>{const index=options.indexOf(item);const current=item._all?displayAll:String(item.id)===String(value);return <button type="button" role="option" aria-selected={current} className={`${current?"selected":""} ${activeIndex===index?"active":""}`} key={item._all?"all":item.id} onMouseEnter={()=>setActiveIndex(index)} onClick={()=>choose(item)}>{item._all?<span className="platform-mark platform-all">全</span>:<PlatformMark platform={item}/>}<span><b>{item._all?allLabel:platformName(item)}</b>{!item._all&&<small>{accessText(item)}</small>}</span>{current&&<i>✓</i>}</button>})}</section>)}
        {!options.length&&<p>没有匹配的平台</p>}
      </div>
    </div>}
  </div>;
}
