import React,{useEffect,useMemo,useRef,useState} from "react";

const EMPTY={id:0,profile_name:"",brand_name:"",industry:"",main_business:"",region:"",called_name:"",gender:"",age:"",role:"",speaking_style:"亲和自然",investment_story:"",years_experience:"",setback_solution:"",trust_reason:"",goal:"",product_intro:"",product_price:"",selling_points:"",differentiation:"",target_customer:"",usage_scenario:"",customer_needs:""};
const LABELS={brand_name:"品牌名称",main_business:"主营业务",region:"所在地区",investment_story:"投入与起步",years_experience:"从业年限",setback_solution:"挫折与解决",trust_reason:"为什么值得信赖",goal:"目标或者期望",called_name:"别人怎么称呼你",gender:"性别",age:"年龄",role:"身份角色",speaking_style:"说话风格",product_intro:"产品介绍",product_price:"产品价格",selling_points:"核心卖点",differentiation:"与同行区别",target_customer:"目标客户",usage_scenario:"使用场景",customer_needs:"用户需求"};
const SUGGESTION_FIELDS=["investment_story","setback_solution","trust_reason","selling_points","differentiation","target_customer","usage_scenario","customer_needs","goal"];
const STYLE_OPTIONS=["专业商务","亲和自然","销售成交","老板口吻","技术专家"];

async function api(path,options={}){const response=await fetch(path,{headers:{"Content-Type":"application/json"},...options,body:options.body&&typeof options.body!=="string"?JSON.stringify(options.body):options.body});const json=await response.json();if(!json.ok)throw Error(json.error||"操作失败");return json.data}
function parseBlocks(raw){const result={};const text=String(raw||"");const pattern=/【([^】]+)】\s*\n([\s\S]*?)(?=\n【[^】]+】|$)/g;let match;while((match=pattern.exec(text)))result[match[1]]=match[2].trim();return result}
function makeBlocks(items){return items.filter(([,value])=>String(value||"").trim()).map(([label,value])=>`【${label}】\n${String(value).trim()}`).join("\n")}
function fromProfile(profile={}){
  const brand=parseBlocks(profile.brand_intro),persona=parseBlocks(profile.persona_info),product=parseBlocks(profile.product_info);
  const structuredBrand=Object.keys(brand).length>0,structuredPersona=Object.keys(persona).length>0,structuredProduct=Object.keys(product).length>0;
  return {...EMPTY,id:profile.id||0,profile_name:profile.company_name||"",brand_name:brand["品牌名称"]||profile.company_name||"",industry:profile.industry||"",main_business:brand["主营业务"]||"",region:brand["所在地区"]||"",investment_story:brand["投入与起步"]||"",years_experience:brand["从业年限"]||"",setback_solution:brand["挫折与解决"]||"",trust_reason:brand["为什么值得信赖"]||(!structuredBrand?profile.brand_intro||"":""),goal:brand["目标或者期望"]||"",called_name:persona["别人怎么称呼你"]||"",gender:persona["性别"]||"",age:persona["年龄"]||"",role:persona["身份角色"]||(!structuredPersona?profile.persona_info||"":""),speaking_style:persona["说话风格"]||"亲和自然",product_intro:product["产品介绍"]||(!structuredProduct?profile.product_info||"":""),product_price:product["产品价格"]||"",selling_points:product["核心卖点"]||"",differentiation:product["与同行区别"]||"",target_customer:product["目标客户"]||"",usage_scenario:product["使用场景"]||"",customer_needs:product["用户需求"]||""};
}
function toProfile(form){return {id:form.id||0,company_name:form.profile_name.trim(),industry:form.industry.trim(),brand_intro:makeBlocks([["品牌名称",form.brand_name],["主营业务",form.main_business],["所在地区",form.region],["投入与起步",form.investment_story],["从业年限",form.years_experience],["挫折与解决",form.setback_solution],["为什么值得信赖",form.trust_reason],["目标或者期望",form.goal]]),persona_info:makeBlocks([["别人怎么称呼你",form.called_name],["性别",form.gender],["年龄",form.age],["身份角色",form.role],["说话风格",form.speaking_style]]),product_info:makeBlocks([["产品介绍",form.product_intro],["产品价格",form.product_price],["核心卖点",form.selling_points],["与同行区别",form.differentiation],["目标客户",form.target_customer],["使用场景",form.usage_scenario],["用户需求",form.customer_needs]])}}

function KnowledgeField({label,value,onChange,placeholder,textarea=false,type="text",required=false,children}){return <label className={`knowledge-field ${required&&!String(value||"").trim()?"missing":""}`}><span>{label}{required?<em>必填</em>:null}</span>{children|| (textarea?<textarea value={value||""} onChange={event=>onChange(event.target.value)} placeholder={placeholder}/>:<input type={type} value={value||""} onChange={event=>onChange(event.target.value)} placeholder={placeholder}/>)}</label>}
function KnowledgeGroup({icon,title,desc,open,onToggle,children,missing,Icon}){return <section className={`knowledge-group ${open?"open":"collapsed"}`}><button className="knowledge-group-head" onClick={onToggle}><i><Icon name={icon} size={17}/></i><span><b>{title}</b><small>{desc}</small></span>{missing?<em>{missing} 项待填写</em>:null}<strong>{open?"−":"+"}</strong></button>{open?<div className="knowledge-group-body">{children}</div>:null}</section>}

export default function AccountKnowledge({db,refresh,notify,Button,Icon}){
  const [activeId,setActiveId]=useState(0),[isNew,setIsNew]=useState(false),[form,setForm]=useState(EMPTY),[dirty,setDirty]=useState(false),[saveState,setSaveState]=useState("saved"),[openGroups,setOpenGroups]=useState({basic:true,persona:true,story:true,product:true,customer:true}),[aiBusy,setAiBusy]=useState(false),[suggestion,setSuggestion]=useState(null),[editSuggestion,setEditSuggestion]=useState(false);
  const formRef=useRef(form),savingRef=useRef(false),pendingSaveRef=useRef(false),versionRef=useRef(0);
  formRef.current=form;
  useEffect(()=>{if(!activeId&&!isNew&&db.profiles[0])setActiveId(db.profiles[0].id)},[db.profiles,activeId,isNew]);
  useEffect(()=>{if(isNew)return;const profile=db.profiles.find(item=>item.id===activeId);if(profile){setForm(fromProfile(profile));setDirty(false);setSaveState("saved");setSuggestion(null)}},[activeId,isNew,db.profiles]);

  const persist=async(value=formRef.current,version=versionRef.current,showNotice=false)=>{
    if(!value.profile_name.trim()){setSaveState("missing");return false}
    if(savingRef.current){pendingSaveRef.current=true;return false}
    savingRef.current=true;setSaveState("saving");
    try{
      const result=await api("/api/profiles",{method:"POST",body:toProfile(value)});
      if(!value.id){setActiveId(result.id);setIsNew(false);setForm(current=>({...current,id:result.id}))}
      if(version===versionRef.current){setDirty(false);setSaveState("saved")}
      else setSaveState("unsaved");
      await refresh(false);
      if(showNotice)notify("AI创作知识库已保存");
      return true;
    }catch(error){setSaveState("error");notify(error.message);return false}
    finally{
      savingRef.current=false;
      if(pendingSaveRef.current){pendingSaveRef.current=false;window.setTimeout(()=>persist(formRef.current,versionRef.current),80)}
    }
  };
  useEffect(()=>{if(!dirty)return;const version=versionRef.current;const timer=window.setTimeout(()=>persist(formRef.current,version),900);return()=>window.clearTimeout(timer)},[form,dirty]);
  const update=(key,value)=>{versionRef.current+=1;setForm(current=>({...current,[key]:value}));setDirty(true);setSaveState("unsaved")};
  const choose=async id=>{if(dirty)await persist(formRef.current,versionRef.current);setIsNew(false);setActiveId(id)};
  const createNew=async()=>{if(dirty)await persist(formRef.current,versionRef.current);setIsNew(true);setActiveId(0);setForm(EMPTY);setDirty(false);setSaveState("saved");setSuggestion(null)};
  const editProfile=async id=>{await choose(id);window.setTimeout(()=>document.querySelector('.knowledge-field input')?.focus(),80)};
  const removeProfile=async profile=>{
    if(!window.confirm(`确定删除档案“${profile.company_name}”吗？删除后无法恢复。`))return;
    if(activeId===profile.id){versionRef.current+=1;setDirty(false);setSaveState("saved")}
    try{
      await api(`/api/profiles/${profile.id}`,{method:"DELETE"});
      const remaining=db.profiles.filter(item=>item.id!==profile.id);
      if(activeId===profile.id){setActiveId(remaining[0]?.id||0);setIsNew(false);if(!remaining.length)setForm(EMPTY)}
      await refresh(false);notify("账号档案已删除");
    }catch(error){notify(error.message)}
  };
  const toggle=key=>setOpenGroups(current=>({...current,[key]:!current[key]}));
  const generateSuggestion=async()=>{
    if(!form.brand_name.trim()||!form.industry.trim()||!form.product_intro.trim()){notify("请先填写品牌名称 行业和产品介绍");return}
    setAiBusy(true);
    try{setSuggestion(await api("/api/profiles/ai-suggestions",{method:"POST",body:form}));setEditSuggestion(false)}catch(error){notify(error.message)}finally{setAiBusy(false)}
  };
  const adoptSuggestion=()=>{versionRef.current+=1;setForm(current=>({...current,...Object.fromEntries(SUGGESTION_FIELDS.map(key=>[key,suggestion?.[key]||current[key]]))}));setDirty(true);setSaveState("unsaved");setSuggestion(null);notify("已采用AI建议，正在自动保存")};
  const changeSuggestion=(key,value)=>setSuggestion(current=>({...current,[key]:value}));

  const requiredKeys=["profile_name","brand_name","industry","main_business","called_name","role","product_intro","selling_points","target_customer","customer_needs"];
  const filled=requiredKeys.filter(key=>String(form[key]||"").trim()).length;
  const missingCount=keys=>keys.filter(key=>!String(form[key]||"").trim()).length;
  const position=useMemo(()=>{
    const years=form.years_experience?`拥有${form.years_experience}经验的`:"专注于";
    const identity=form.role||`${form.industry||"当前行业"}从业者`;
    return `你是${form.region?form.region+"的":"一家"}${years}${form.brand_name||form.profile_name||"品牌"}，以${identity}身份分享${form.product_intro||form.main_business||"产品与行业经验"}。`;
  },[form]);
  const directions=useMemo(()=>String(form.selling_points||"").split(/[\n、，,；;]/).map(item=>item.trim()).filter(Boolean).slice(0,4),[form.selling_points]);
  const statusText={saved:"已自动保存",saving:"正在保存…",unsaved:"等待自动保存",missing:"填写档案名称后自动保存",error:"保存失败"}[saveState];

  return <div className="page account-knowledge-page">
    <header className="knowledge-heading"><div><span>AI CREATION KNOWLEDGE</span><h1>AI创作知识库</h1><p>让AI理解你的品牌、人物、产品和客户，统一后续视频创作方向。</p></div><div className="knowledge-head-actions"><span className={`knowledge-save-state ${saveState}`}><i/>{statusText}</span><Button quiet icon="plus" onClick={createNew}>新建档案</Button><Button primary icon="magic" disabled={aiBusy} onClick={generateSuggestion}>{aiBusy?"AI生成中":"AI生成填写建议"}</Button></div></header>
    <div className="knowledge-progress"><div><span>档案完整度</span><b>{filled}/{requiredKeys.length}</b></div><i><em style={{width:`${filled/requiredKeys.length*100}%`}}/></i><p>{filled===requiredKeys.length?"核心资料已完整，可以用于AI创作":"补充标记为必填的资料，AI生成会更准确"}</p></div>

    <div className="knowledge-layout">
      <aside className="knowledge-sidebar"><div className="knowledge-sidebar-head"><div><b>账号档案</b><small>{db.profiles.length} 个知识库</small></div><button onClick={createNew}><Icon name="plus" size={17}/></button></div><div className="knowledge-profile-list">{isNew?<div className="knowledge-profile-item active new"><button className="knowledge-profile-select"><i>新</i><span><b>{form.profile_name||"未命名档案"}</b><small>正在创建</small></span></button></div>:null}{db.profiles.length?db.profiles.map(profile=><div className={`knowledge-profile-item ${!isNew&&activeId===profile.id?"active":""}`} key={profile.id}><button className="knowledge-profile-select" onClick={()=>choose(profile.id)}><i>{String(profile.company_name||"档").slice(0,1)}</i><span><b>{profile.company_name}</b><small>{profile.industry||"行业待填写"}</small></span><em>{!isNew&&activeId===profile.id?"当前":""}</em></button><div className="knowledge-profile-actions"><button onClick={()=>editProfile(profile.id)}>修改</button><button className="delete" onClick={()=>removeProfile(profile)}>删除</button></div></div>):!isNew?<div className="knowledge-profile-empty"><Icon name="user" size={27}/><b>还没有账号档案</b><small>创建后AI才能理解你的品牌</small><button onClick={createNew}>创建第一个档案</button></div>:null}</div><div className="knowledge-sidebar-note"><Icon name="magic" size={17}/><p>档案内容会作为AI改写、口播、标题和批量生产的创作依据。</p></div></aside>

      <main className="knowledge-main">
        <KnowledgeGroup Icon={Icon} icon="user" title="基础资料" desc="账号与品牌的基本身份" open={openGroups.basic} onToggle={()=>toggle("basic")} missing={missingCount(["profile_name","brand_name","industry","main_business"])}><div className="knowledge-fields three"><KnowledgeField required label="档案名称" value={form.profile_name} onChange={value=>update("profile_name",value)} placeholder="例如：亿康源洗护工厂账号"/><KnowledgeField required label="品牌名称" value={form.brand_name} onChange={value=>update("brand_name",value)} placeholder="例如：亿康源"/><KnowledgeField required label="行业" value={form.industry} onChange={value=>update("industry",value)} placeholder="例如：洗护用品OEM/ODM"/><KnowledgeField required label="主营业务" value={form.main_business} onChange={value=>update("main_business",value)} placeholder="例如：洗发水、沐浴露和护肤品定制生产"/><KnowledgeField label="所在地区" value={form.region} onChange={value=>update("region",value)} placeholder="例如：广东广州"/></div></KnowledgeGroup>

        <KnowledgeGroup Icon={Icon} icon="user" title="AI人物设定" desc="决定口播人格、措辞和交流方式" open={openGroups.persona} onToggle={()=>toggle("persona")} missing={missingCount(["called_name","role"])}><div className="knowledge-fields three"><KnowledgeField required label="别人怎么称呼你" value={form.called_name} onChange={value=>update("called_name",value)} placeholder="例如：康哥、李总、张厂长"/><KnowledgeField label="性别" value={form.gender} onChange={value=>update("gender",value)}><select value={form.gender} onChange={event=>update("gender",event.target.value)}><option value="">请选择</option><option>男</option><option>女</option><option>不限定</option></select></KnowledgeField><KnowledgeField label="年龄" type="number" value={form.age} onChange={value=>update("age",value)} placeholder="例如：38"/><KnowledgeField required label="身份角色" value={form.role} onChange={value=>update("role",value)} placeholder="例如：洗护工厂老板、产品研发负责人"/></div><div className="knowledge-style"><span>说话风格</span><div>{STYLE_OPTIONS.map(style=><button className={form.speaking_style===style?"active":""} onClick={()=>update("speaking_style",style)} key={style}><i/>{style}</button>)}</div></div></KnowledgeGroup>

        <KnowledgeGroup Icon={Icon} icon="layers" title="品牌故事" desc="沉淀经历、信任基础与长期目标" open={openGroups.story} onToggle={()=>toggle("story")}><div className="knowledge-fields two"><KnowledgeField textarea label="投了多少钱干了什么事" value={form.investment_story} onChange={value=>update("investment_story",value)} placeholder="例如：投入50万元建立生产车间，从原料采购、研发到生产包装形成完整体系。"/><KnowledgeField textarea label="做了多少年" value={form.years_experience} onChange={value=>update("years_experience",value)} placeholder="例如：从事洗护行业15年，专注OEM/ODM产品研发和生产。"/><KnowledgeField textarea label="遇到什么挫折怎么解决的" value={form.setback_solution} onChange={value=>update("setback_solution",value)} placeholder="例如：创业初期遇到供应链不稳定，通过建立稳定供应体系和严格质量管理解决。"/><KnowledgeField textarea label="为什么值得信赖" value={form.trust_reason} onChange={value=>update("trust_reason",value)} placeholder="例如：拥有多年行业经验，坚持品质管理，服务多个品牌客户。"/><KnowledgeField textarea label="目标或者期望" value={form.goal} onChange={value=>update("goal",value)} placeholder="例如：帮助更多品牌快速打造自己的产品。"/></div></KnowledgeGroup>

        <KnowledgeGroup Icon={Icon} icon="film" title="产品信息" desc="AI创作时优先引用的产品事实" open={openGroups.product} onToggle={()=>toggle("product")} missing={missingCount(["product_intro","selling_points"])}><div className="knowledge-fields two"><KnowledgeField required textarea label="产品介绍" value={form.product_intro} onChange={value=>update("product_intro",value)} placeholder="例如：主营洗发水、沐浴露、护肤产品，支持定制，根据订单量提供报价。"/><KnowledgeField textarea label="产品价格" value={form.product_price} onChange={value=>update("product_price",value)} placeholder="例如：根据配方、包装和订单数量提供阶梯报价。"/><KnowledgeField required textarea label="核心卖点" value={form.selling_points} onChange={value=>update("selling_points",value)} placeholder="例如：源头工厂、支持OEM/ODM定制、小批量生产、快速交付。"/><KnowledgeField textarea label="与同行区别" value={form.differentiation} onChange={value=>update("differentiation",value)} placeholder="例如：研发、生产和包装一体化，沟通和交付效率更高。"/></div></KnowledgeGroup>

        <KnowledgeGroup Icon={Icon} icon="tasks" title="客户画像" desc="明确内容写给谁、在什么场景解决什么问题" open={openGroups.customer} onToggle={()=>toggle("customer")} missing={missingCount(["target_customer","customer_needs"])}><div className="knowledge-fields three"><KnowledgeField required textarea label="目标客户" value={form.target_customer} onChange={value=>update("target_customer",value)} placeholder="例如：品牌方、电商卖家、创业者、美妆行业采购。"/><KnowledgeField textarea label="使用场景" value={form.usage_scenario} onChange={value=>update("usage_scenario",value)} placeholder="例如：新品开发、直播电商备货、品牌升级和渠道定制。"/><KnowledgeField required textarea label="用户需求" value={form.customer_needs} onChange={value=>update("customer_needs",value)} placeholder="例如：降低起订量、稳定品质、缩短交付周期、提供配方建议。"/></div></KnowledgeGroup>

        {suggestion?<section className="knowledge-ai-suggestion"><header><div><i><Icon name="magic" size={17}/></i><span><b>AI填写建议</b><small>建议暂未写入档案，确认采用后才会自动保存</small></span></div><em>ASYNC RESULT</em></header><div className="knowledge-suggestion-grid">{SUGGESTION_FIELDS.map(key=><label key={key}><span>{LABELS[key]}</span>{editSuggestion?<textarea value={suggestion[key]||""} onChange={event=>changeSuggestion(key,event.target.value)}/>:<p>{suggestion[key]||"暂无建议"}</p>}</label>)}</div>{suggestion.current_positioning?<blockquote><b>建议定位</b>{suggestion.current_positioning}</blockquote>:null}<footer><Button quiet disabled={aiBusy} onClick={generateSuggestion}>重新生成</Button><Button quiet onClick={()=>setEditSuggestion(value=>!value)}>{editSuggestion?"完成修改":"手动修改"}</Button><Button primary icon="check" onClick={adoptSuggestion}>采用建议</Button></footer></section>:null}

        <section className="knowledge-ai-preview"><header><div><span>AI UNDERSTANDING</span><h2>AI理解结果</h2></div><small>根据当前填写内容实时预览</small></header><div className="knowledge-preview-body"><div className="knowledge-position"><span>当前AI定位</span><p>{position}</p><small>继续补充品牌经历和客户需求，定位会更具体。</small></div><div className="knowledge-effects"><b>当前档案会影响</b>{[["edit","AI改写风格"],["waveform","视频口播语气"],["caption","标题方向"],["send","营销重点"]].map(([icon,text])=><span key={text}><i><Icon name="check" size={12}/></i>{text}</span>)}</div><div className="knowledge-directions"><b>内容突出方向</b>{directions.length?directions.map(item=><span key={item}>{item}</span>):<small>填写核心卖点后自动生成方向标签</small>}</div></div></section>
        <div className="knowledge-bottom-save"><span className={`knowledge-save-state ${saveState}`}><i/>{statusText}</span><Button quiet onClick={()=>persist(formRef.current,versionRef.current,true)}>立即保存</Button></div>
      </main>
    </div>
  </div>;
}
