// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;
import "./ResearchMixedExecutor.sol";

// Separately compiled instrument repair: canonical Base Pancake V2 only.
// Same original signals, route currencies, input amount and holding interval.
contract ResearchPancakeV2Executor is ResearchMixedExecutor {
    address constant PANCAKE=0x02a84c1b3BBD7401a5f7fa98a384EBC70bB5749E;
    function registered(ResearchStep memory s) internal view override {
        ResearchPool p=ResearchPool(s.pool);
        if(s.kind!=2 || p.factory()!=PANCAKE) {super.registered(s);return;}
        require(p.token0()==s.key.currency0 && p.token1()==s.key.currency1,"currencies");
        (bool ok,bytes memory raw)=PANCAKE.staticcall(abi.encodeWithSignature(
            "getPair(address,address)",s.key.currency0,s.key.currency1));
        require(ok && raw.length==32 && abi.decode(raw,(address))==s.pool,"unregistered Pancake pool");
    }
    function constantProductOutput(address pool,uint256 netInput,uint256 rin,uint256 rout)
        internal view override returns(uint256)
    {
        if(ResearchPool(pool).factory()!=PANCAKE) return super.constantProductOutput(pool,netInput,rin,rout);
        uint256 adjusted=netInput*9975;
        return adjusted*rout/(rin*10000+adjusted);
    }
}
