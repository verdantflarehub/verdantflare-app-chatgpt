import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';

const source=readFileSync(new URL('../dashboard/dashboard.mjs',import.meta.url),'utf8');
const route=source.slice(source.indexOf('async function route('),source.indexOf('async function mutate('));
const options=source.split('\n').filter(line=>/^function (proxyValue|proxyOptions)\(/.test(line)).join('\n');
const requests=[];
const context=vm.createContext({
  controller:null,timer:null,generation:0,readFailures:0,editor:null,page:'',views:new Map(),
  search:'',filter:'all',offset:0,accountFilter:'',fromDate:'',toDate:'',media:'',
  disposed:false,proxyItems:[],proxyLoadError:false,accounts:[],operation:null,
  window:{scrollY:0},crypto:{randomUUID:()=> 'synthetic-operation'},clearTimeout(){},
  $:()=>({focus(){}}),notice:()=>{},errorText:()=>'',accountView:a=>a,
  esc:value=>String(value??''),
  call:async(method,path)=>{
    requests.push([method,path]);
    if(path==='/proxies')return {items:[{proxy_id:'fixture',revision:4,name:'Authorized proxy',status:'active'}]};
    return {id:'fixture-account',proxy:'fixture',proxyRevision:4};
  },render(){},
});
vm.runInContext(route+'\n'+options,context);
await context.route({path:'/accounts/new'});
assert.deepEqual(requests,[['GET','/proxies']]);
assert.match(context.proxyOptions({proxy:'none'}),/Authorized proxy/);
assert.equal(context.proxyLoadError,false);

requests.length=0;
await context.route({path:'/accounts/abc/edit'});
assert.deepEqual(requests,[['GET','/proxies'],['GET','/accounts/abc']]);
assert.match(context.proxyOptions(context.accounts[0]),/value="fixture:4" selected/);

context.call=async(method,path)=>{
  if(path==='/proxies')throw new Error('offline');
  return {id:'fixture-account',proxy:'fixture',proxyRevision:4};
};
await context.route({path:'/accounts/abc/edit'});
assert.equal(context.proxyLoadError,true);
assert.match(context.proxyOptions(context.accounts[0]),/value="fixture:4" selected>当前代理（暂不可用）/);
console.log('Dashboard route: new/edit fetch authorized proxies; failure preserves selected reference');
