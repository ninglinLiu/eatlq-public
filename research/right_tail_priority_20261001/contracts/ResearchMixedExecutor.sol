// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;
import "./ResearchV4Executor.sol";

interface ResearchPool {
    function token0() external view returns(address);
    function token1() external view returns(address);
    function factory() external view returns(address);
    function fee() external view returns(uint24);
    function tickSpacing() external view returns(int24);
    function stable() external view returns(bool);
    function getAmountOut(uint256,address) external view returns(uint256);
    function getReserves() external view returns(uint112,uint112,uint32);
    function swap(address,bool,int256,uint160,bytes calldata) external returns(int256,int256);
    function swap(uint256,uint256,address,bytes calldata) external;
}
struct ResearchStep {uint8 kind; address pool; Key key; bool zeroForOne;}

// Read-only historical research instrument, NOT an audited production router.
// Every pool is checked against an allowlisted factory's own registry, not only
// a pool's self-reported factory. Actual balance deltas include transfer taxes.
contract ResearchMixedExecutor {
    address constant PM=0x498581fF718922c3f8e6A244956aF099B2652b2b;
    address constant WETH=0x4200000000000000000000000000000000000006;
    address constant UNI2=0x8909Dc15e40173Ff4699343b6eB8132c65e18eC6;
    address constant UNI3=0x33128a8fC17869897dcE68Ed026d694621f6FDfD;
    uint160 constant MIN_SQRT=4295128739;
    uint160 constant MAX_SQRT=1461446703485210103287273052203988822378723970342;
    address private caller;
    address private pendingPool;
    address private pendingInput;
    bool private pendingDirection;
    uint256 private pendingMaximum;
    bool private pendingV4;
    receive() external payable {}
    function msgSender() external view returns(address) {return caller;}
    function balance(address c) public view returns(uint256) {
        return c==address(0) ? address(this).balance : Token(c).balanceOf(address(this));
    }
    function pay(address c,address to,uint256 amount) internal {
        (bool ok,bytes memory r)=c.call(abi.encodeWithSignature("transfer(address,uint256)",to,amount));
        require(ok && (r.length==0 || abi.decode(r,(bool))),"payment");
    }
    function registered(ResearchStep memory s) internal view virtual {
        ResearchPool p=ResearchPool(s.pool);
        require(p.token0()==s.key.currency0 && p.token1()==s.key.currency1,"currencies");
        address f=p.factory();bytes memory data;
        if(s.kind==2) {
            require(f==UNI2,"unsupported V2 factory");
            data=abi.encodeWithSignature("getPair(address,address)",s.key.currency0,s.key.currency1);
        } else if(s.kind==5) {
            require(f==0x420DD381b31aEf6683db6B902084cB0FFECe40Da,"unsupported Aero V2 factory");
            data=abi.encodeWithSignature("getPool(address,address,bool)",s.key.currency0,s.key.currency1,p.stable());
        } else if(s.kind==6) {
            // Hydrex official SDK deployer + init-code hash is an independent
            // CREATE2 identity check. QuickSwap uses its verified Base factory.
            address expected=address(uint160(uint256(keccak256(abi.encodePacked(bytes1(0xff),
                address(0x1595A5D101d69D2a2bAB2976839cC8eeEb13Ab94),
                keccak256(abi.encode(s.key.currency0,s.key.currency1)),
                bytes32(0xa18736c3ee97fe3c96c9428c0cc2a9116facec18e84f95f9da30543f8238a782))))));
            require(s.pool==expected || f==0xC5396866754799B9720125B104AE01d935Ab9C7b,"unsupported Algebra deployment");
            data=abi.encodeWithSignature("poolByPair(address,address)",s.key.currency0,s.key.currency1);
        } else if(f==UNI3) {
            data=abi.encodeWithSignature("getPool(address,address,uint24)",s.key.currency0,s.key.currency1,p.fee());
        } else {
            require(f==0x5e7BB104d84c7CB9B682AaC2F3d509f5F406809A ||
                    f==0xaDe65c38CD4849aDBA595a4323a8C7DdfE89716a ||
                    f==0xf8f2eB4940CFE7d13603DDDD87f123820Fc061Ef,"unsupported V3 factory");
            data=abi.encodeWithSignature("getPool(address,address,int24)",s.key.currency0,s.key.currency1,p.tickSpacing());
        }
        (bool ok,bytes memory raw)=f.staticcall(data);
        require(ok && raw.length==32 && abi.decode(raw,(address))==s.pool,"unregistered pool");
    }
    function constantProductOutput(address,uint256 netInput,uint256 rin,uint256 rout) internal view virtual returns(uint256) {
        uint256 adjusted=netInput*997;
        return adjusted*rout/(rin*1000+adjusted);
    }
    function run(ResearchStep[] calldata steps,uint128 amount,bool wrapInput) external payable
        returns(uint256 spent,uint256 received,uint256 measuredGas,uint256 baseFee,uint256 timestamp,uint256[] memory leftover)
    {
        uint256 start=gasleft();
        require(caller==address(0) && steps.length>0 && steps.length<=4,"route");caller=msg.sender;
        address input=steps[0].zeroForOne ? steps[0].key.currency0 : steps[0].key.currency1;
        ResearchStep memory last=steps[steps.length-1];
        address output=last.zeroForOne ? last.key.currency1 : last.key.currency0;
        require(input!=output,"cycle");
        uint256 beforeInput=balance(input);
        if(wrapInput) {require(input==WETH && msg.value==amount,"wrap");Wrapped(WETH).deposit{value:amount}();beforeInput+=amount;}
        uint256 beforeOutput=balance(output);uint256 qty=amount;
        leftover=new uint256[](steps.length);
        for(uint256 i;i<steps.length;i++) {
            ResearchStep memory s=steps[i];
            address tin=s.zeroForOne ? s.key.currency0 : s.key.currency1;
            address tout=s.zeroForOne ? s.key.currency1 : s.key.currency0;
            if(i>0) require(tin==(steps[i-1].zeroForOne ? steps[i-1].key.currency1 : steps[i-1].key.currency0),"join");
            require(qty>0 && qty<=type(uint128).max,"quantity");
            uint256 preIn=balance(tin);uint256 preOut=balance(tout);
            if(s.kind==4) {pendingV4=true;Manager(PM).unlock(abi.encode(s,qty));pendingV4=false;}
            else if(s.kind==3 || s.kind==6) {
                registered(s);pendingPool=s.pool;pendingInput=tin;pendingDirection=s.zeroForOne;pendingMaximum=qty;
                ResearchPool(s.pool).swap(address(this),s.zeroForOne,int256(qty),s.zeroForOne ? MIN_SQRT+1 : MAX_SQRT-1,"");
                pendingPool=address(0);pendingMaximum=0;
            } else if(s.kind==2 || s.kind==5) {
                registered(s);
                // Only the transfer's own received amount is attributed to us;
                // pre-existing pool donations are not fabricated as our input.
                uint256 prePool=Token(tin).balanceOf(s.pool);pay(tin,s.pool,qty);
                uint256 netInput=Token(tin).balanceOf(s.pool)-prePool;
                uint256 out;
                if(s.kind==2) {
                    (uint112 r0,uint112 r1,)=ResearchPool(s.pool).getReserves();
                    (uint256 rin,uint256 rout)=s.zeroForOne ? (r0,r1) : (r1,r0);
                    out=constantProductOutput(s.pool,netInput,rin,rout);
                } else out=ResearchPool(s.pool).getAmountOut(netInput,tin);
                ResearchPool(s.pool).swap(s.zeroForOne ? 0 : out,s.zeroForOne ? out : 0,address(this),"");
            } else revert("protocol");
            uint256 paid=preIn-balance(tin);require(paid<=qty,"overpayment");
            leftover[i]=qty-paid;qty=balance(tout)-preOut;
        }
        spent=beforeInput-balance(input);received=balance(output)-beforeOutput;
        caller=address(0);measuredGas=start-gasleft();return(spent,received,measuredGas,block.basefee,block.timestamp,leftover);
    }
    function uniswapV3SwapCallback(int256 d0,int256 d1,bytes calldata) external {
        settleConcentrated(d0,d1);
    }
    function algebraSwapCallback(int256 d0,int256 d1,bytes calldata) external {
        settleConcentrated(d0,d1);
    }
    function settleConcentrated(int256 d0,int256 d1) internal {
        require(caller!=address(0) && msg.sender==pendingPool,"callback");
        int256 debt=pendingDirection ? d0 : d1;int256 out=pendingDirection ? d1 : d0;
        require(debt>=0 && out<=0 && uint256(debt)<=pendingMaximum,"debt");
        pendingMaximum-=uint256(debt);pay(pendingInput,msg.sender,uint256(debt));
    }
    function unlockCallback(bytes calldata data) external returns(bytes memory) {
        require(msg.sender==PM && pendingV4 && caller!=address(0),"callback");
        (ResearchStep memory s,uint256 qty)=abi.decode(data,(ResearchStep,uint256));
        int256 d=Manager(PM).swap(s.key,SwapParams(s.zeroForOne,-int256(qty),s.zeroForOne ? MIN_SQRT+1 : MAX_SQRT-1),"");
        int128 d0=int128(d>>128);int128 d1=int128(d);
        int128 din=s.zeroForOne ? d0 : d1;int128 dout=s.zeroForOne ? d1 : d0;
        require(din<=0 && dout>=0,"sign");
        address tin=s.zeroForOne ? s.key.currency0 : s.key.currency1;
        address tout=s.zeroForOne ? s.key.currency1 : s.key.currency0;
        uint256 debt=uint256(-int256(din));Manager(PM).sync(tin);
        if(tin==address(0)) Manager(PM).settle{value:debt}();
        else {pay(tin,PM,debt);Manager(PM).settle();}
        Manager(PM).take(tout,address(this),uint256(uint128(dout)));return "";
    }
}
