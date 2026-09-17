#!/usr/bin/env python3
import pandas as pd,argparse,re,os
MEAT=['beef','chicken','pork','turkey','ham','lamb','fish','salmon','cod','shrimp','tilapia','mahi','tuna','sausage','meatball','steak','brisket','rib','roast','duck','halal']
IGNORE=['rice','bean','beans','vegetable','broccoli','carrot','cabbage','salad','soup','bread','bun','tortilla','shell','potato','fries','pasta','sauce','tofu','vegan','impossible','beyond','jasmine']
ENTREE=['roasted','grilled','braised','bbq','teriyaki','stir','fried','baked','fajita','verde','marsala','piccata','curry','adobo','herb','lemon']
CFG={'XRDS':{'header':'Center Plate','abbr':'XRDS'},'C3':{'header':'Center Plate','abbr':'C3'},'CKC':{'header':'Entree','abbr':'CKC'},'FTH':{'header':'Chef\'s Table','abbr':'FTH'}}
DAYS=['monday','tuesday','wednesday','thursday','friday','saturday','sunday']
STOP=['center plate','entree','chef\'s table','iron & ember','fire & flour','week']
def sc(t):
 t=t.lower();return 3*sum(k in t for k in MEAT)-2*sum(k in t for k in IGNORE)+sum(k in t for k in ENTREE)
def clean(inp,hall,meal,out=None):
 df=pd.read_excel(inp);nc=[c for c in df.columns if c.lower()=='name'][0];week=day=None;res=[]
 for i,r in df.iterrows():
  v=str(r[nc]).strip();l=v.lower()
  if re.match(r'week\s*\d+',l): week=v;continue
  if l in DAYS: day=v.title();continue
  if l==CFG[hall]['header'].lower():
   best=None;bs=-999
   for j in range(i+1,min(i+13,len(df))):
    it=str(df.iloc[j][nc]).strip();il=it.lower()
    if any(il.startswith(s) for s in STOP):break
    s=sc(it)
    if s>bs:bs=s;best=df.iloc[j]
   if best is not None and bs>0:
    get=lambda names: next((best[n] for n in names if n in best.index),None)
    res.append({'Week':week,'Day':day,'Meal':meal,'Dining Hall':CFG[hall]['abbr'],'Name':best[nc],'Size of portion (oz)':get(['Size of portion (oz)']),'Planned portions':get(['Planned portions']),'Planned weight (lb)':get(['Planned weight (lb)']),'Cost (exc.tax) ($)':get(['Cost (exc.tax) ($)','Price (exc.VAT) ($)','Cost'])})
 outdf=pd.DataFrame(res);out=out or os.path.splitext(inp)[0]+'_cleaned.csv';outdf.to_csv(out,index=False);print(out)
if __name__=='__main__':
 ap=argparse.ArgumentParser();ap.add_argument('input');ap.add_argument('--hall',choices=CFG);ap.add_argument('--meal',choices=['Lunch','Dinner']);ap.add_argument('-o','--output');a=ap.parse_args();clean(a.input,a.hall,a.meal,a.output)
