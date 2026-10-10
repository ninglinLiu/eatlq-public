// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;
import "./ResearchMixedExecutor.t.sol";
import "../contracts/ResearchPancakeV2Executor.sol";

contract MockResearchPancake {
    address public token0;address public token1;
    address public factory=0x02a84c1b3BBD7401a5f7fa98a384EBC70bB5749E;
    constructor(address a,address b) {token0=a;token1=b;}
    function getReserves() external pure returns(uint112,uint112,uint32) {return (100 ether,100 ether,0);}
    function swap(uint256 out0,uint256 out1,address to,bytes calldata) external {
        if(out0>0) MockToken(token0).transfer(to,out0);
        if(out1>0) MockToken(token1).transfer(to,out1);
        uint256 b0=Token(token0).balanceOf(address(this));uint256 b1=Token(token1).balanceOf(address(this));
        uint256 in0=b0>100 ether-out0 ? b0-(100 ether-out0) : 0;
        uint256 in1=b1>100 ether-out1 ? b1-(100 ether-out1) : 0;
        require((b0*10000-in0*25)*(b1*10000-in1*25)>=uint256(100 ether)**2*10000**2,"Pancake K");
    }
}
contract ResearchPancakeV2ExecutorTest {
    Vm constant vm=Vm(address(uint160(uint256(keccak256("hevm cheat code")))));
    address constant WETH=0x4200000000000000000000000000000000000006;
    address constant FACTORY=0x02a84c1b3BBD7401a5f7fa98a384EBC70bB5749E;
    ResearchPancakeV2Executor executor;MockToken token;MockResearchPancake pool;
    function setUp() public {
        vm.etch(WETH,address(new MockToken()).code);vm.etch(FACTORY,address(new MockResearchFactory()).code);
        token=new MockToken();executor=new ResearchPancakeV2Executor();pool=new MockResearchPancake(WETH,address(token));
        MockResearchFactory(FACTORY).seed(address(pool));
        MockToken(WETH).seed(address(pool),100 ether);token.seed(address(pool),100 ether);
        vm.deal(address(this),10 ether);
    }
    function route() internal view returns(ResearchStep[] memory steps) {
        steps=new ResearchStep[](1);steps[0]=ResearchStep(2,address(pool),Key(WETH,address(token),0,0,address(0)),true);
    }
    function testUses25BpsNot30Bps() public {
        (uint256 spent,uint256 received,,,,)=executor.run{value:1 ether}(route(),uint128(1 ether),true);
        uint256 adjusted=uint256(1 ether)*9975;
        require(spent==1 ether && received==adjusted*100 ether/(100 ether*10000+adjusted),"fee mismatch");
    }
    function testRegistryMismatchRejected() public {
        MockResearchFactory(FACTORY).seed(address(0));
        try executor.run{value:1 ether}(route(),uint128(1 ether),true) {revert("must reject");} catch {}
    }
}
