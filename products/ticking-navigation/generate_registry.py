"""Build a counted executable registry, not a count of input paraphrases. Remote only."""
import json,sys,hashlib,time,collections
from pathlib import Path
import argparse
parser=argparse.ArgumentParser(description='Compile the reviewed Ticking product command registry')
parser.add_argument('--assistant-spec',type=Path,required=True)
parser.add_argument('--chart-rules',type=Path,required=True)
parser.add_argument('--catalog',type=Path,required=True)
parser.add_argument('--period-studies',type=Path,required=True)
parser.add_argument('--output',type=Path,required=True)
parser.add_argument('--minimum-capabilities',type=int,default=1)
a=parser.parse_args();O=a.output;N=O/'native';P=O/'economic_machine'
P.mkdir(parents=True,exist_ok=True);N.mkdir(exist_ok=True)
spec=json.loads(a.assistant_spec.read_text());rules=json.loads(a.chart_rules.read_text());rows=json.loads(a.catalog.read_text())['rows']
commands=[];phrases={};ambiguous=set()
def norm(s):return ' '.join(s.lower().split())
def add(id,kind,terms,**kw):
 i=len(commands);commands.append({'id':id,'kind':kind,'examples':list(dict.fromkeys(terms))[:3],**kw})
 for s in terms:
  k=norm(s)
  if k in phrases and phrases[k]!=i:ambiguous.add(k)
  else:phrases[k]=i
 return i
for c in spec['conversation']:add('conversation:'+c['id'],'conversation',c['terms'],result={'state':'message','message':c['message'],'choices':c.get('choices',[])})
for p in spec['pages']:add('page:'+p['id'],'navigation',p['terms'],result={'state':'ready','title':p['title'],'route':p['route']})
# All aliases point to an exact currently listed id; no exchange/equity inference.
for row in rows:
 aliases=[k for k,v in spec['aliases'].items() if v==row['id']]
 names=list(dict.fromkeys([row['id'],row['symbol'],row['name'],*aliases]));names=[n for n in names if n]
 for mode in ('chart','terminal'):
  terms=[]
  for n in names:
   terms.extend(([n,n+' chart','chart '+n,n+' 차트',n+' 그래프'] if mode=='chart' else [n+' pro',n+' terminal',n+' pro terminal','terminal '+n,n+' 프로',n+' 프로 터미널','프로 '+n]))
  add('navigate:'+mode+':'+row['id'],'navigation',terms,asset=row['id'],result={'state':'ready','title':row['name']+' · '+row['symbol'],'route':('#/token/' if mode=='chart' else '#/terminal/')+row['id'],'message':'Opening '+row['name']+' · '+row['symbol']+'.'})
# Checked against actual indicator definitions, not assumed period fields.
periods=json.loads(a.period_studies.read_text())
for group,tool in [('studies','study'),('drawings','drawing'),('drawing_tool','drawing_tool'),('panels','panel'),('view','view'),('interval','interval')]:
 for val,names in rules[group].items():
  if tool=='drawing' and val=='level':continue # variable explicit price, existing validator
  if tool=='drawing_tool' and val in {'fib','parallel','regression','long','short'}:terms=['use tool '+n for n in names]
  elif tool=='drawing_tool':terms=['use tool '+n for n in names]+([val] if val not in rules['drawings'] else [])
  else:terms=list(dict.fromkeys([val,*names]))
  if tool=='panel':terms += [val+' panel']
  if tool=='interval':
   unit={'s':'초','m':'분','h':'시간','d':'일','w':'주'}[val[-1]];terms += [val[:-1]+unit,val[:-1]+unit+'봉',val[:-1]+unit+'봉으로',val[:-1]+unit+'봉에서',val[:-1]+unit+'봉에']
  add(tool+':'+val,'chart',terms,actions=[{'tool':tool,'value':val,'parameter':None}])
  if tool=='study' and val in periods:
   for period in [7,9,14,20,50,100,200]:
    add('study:'+val+':'+str(period),'chart',[f'{n} {period}' for n in names]+[f'{period} {n}' for n in names],actions=[{'tool':'study','value':val,'parameter':period}])
add('undo:last','chart',['undo','undo last','되돌려','되돌려줘','되돌리기'],actions=[{'tool':'undo','value':'last','parameter':None}])
# Distinct configurable output parameters, separately counted from tool families.
for color,hex in [('red','#ef5350'),('blue','#4285f4'),('green','#26a69a'),('yellow','#f4c542')]:
 for line in ('upper','middle','lower','all'):
  add('style:bb:'+line+':'+color,'chart',[f'set bollinger {line} color to {color}',f'bb {line} {color}'],actions=[{'tool':'study_style','value':'bb.'+line,'parameter':hex}])
# A collision never silently picks another identity or tool. Existing resolver
# can ask for a choice; explicit canonical commands remain unambiguous.
for k in ambiguous:phrases.pop(k,None)
# Preserve exact IDs ahead of a coincident symbol/name, independently of row order.
for i,c in enumerate(commands):
 if c['id'].startswith('navigate:chart:'):phrases[norm(c['asset'])]=i
 if c['id'].startswith('navigate:terminal:'):phrases[norm(c['asset']+' pro')]=i
assert len(commands)==len({c['id'] for c in commands})
# Deduplicate by actual output including asset / parameter, not labels or aliases.
signatures=[json.dumps({k:v for k,v in c.items() if k not in ('id','examples')},sort_keys=True) for c in commands];assert len(set(signatures))==len(commands)
assert len(commands)>=a.minimum_capabilities
payload={'version':spec['version'],'commands':commands};(P/'commands.json').write_text(json.dumps(payload,ensure_ascii=False,separators=(',',':')))
header='#pragma once\nstruct Phrase { std::string_view text; uint32_t id; };\nstatic constexpr Phrase PHRASES[]={\n'+''.join('{'+json.dumps(k,ensure_ascii=False)+','+str(v)+'},\n' for k,v in sorted(phrases.items(),key=lambda kv:kv[0].encode()))+'};\nstatic constexpr uint8_t COMMAND_KINDS[]={'+','.join('2' if c['kind']=='chart' and c['actions'][0]['tool']=='interval' else '1' if c['kind']=='chart' else '0' for c in commands)+'};\n'
header+='static constexpr uint8_t COMMAND_ASSETS[]={'+','.join('1' if c['id'].startswith('navigate:chart:') else '0' for c in commands)+'};\n'
(N/'commands.generated.hpp').write_text(header)
(O/'native-phrases.json').write_text(json.dumps(phrases,ensure_ascii=False))
report={'capabilities':len(commands),'categories':dict(collections.Counter(c['kind'] for c in commands)),'distinctAssets':len(rows),'inputPhrases':len(phrases),'ambiguousPhrasesExcluded':len(ambiguous),'chartToolFamilies':len({a['tool']+':'+a['value'] for c in commands for a in c.get('actions',[])}),'note':'Navigation destinations and configured study presets are distinct operations, not 1000 independent algorithms.'}
(O/'registry.json').write_text(json.dumps(report,indent=2));print(json.dumps(report))
