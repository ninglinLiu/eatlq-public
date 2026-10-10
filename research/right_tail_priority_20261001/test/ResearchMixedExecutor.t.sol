// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;
import "./ResearchV4Executor.t.sol";
import "../contracts/ResearchMixedExecutor.sol";

contract MockResearchFactory {
    address public result;
    function seed(address p) external {result=p;}
    function getPool(address,address,uint24) external view returns(address) {return result;}
    function getPool(address,address,bool) external view returns(address) {return result;}
    function getPair(address,address) external view returns(address) {return result;}
    function poolByPair(address,address) external view returns(address) {return result;}
}
contract MockResearchV3 {
    address public token0;address public token1;
    address public factory=0x33128a8fC17869897dcE68Ed026d694621f6FDfD;
    uint24 public fee=3000;uint256 public fillBps=10000;
    bool public algebraStyle;
    constructor(address a,address b) {token0=a;token1=b;}
    function setFill(uint256 bps) external {fillBps=bps;}
    function setAlgebra() external {factory=0xC5396866754799B9720125B104AE01d935Ab9C7b;algebraStyle=true;}
    function swap(address to,bool z,int256 amount,uint160,bytes calldata) external returns(int256 a,int256 b) {
        uint256 q=uint256(amount)*fillBps/10000;
        address tin=z ? token0 : token1;address tout=z ? token1 : token0;
        uint256 before=Token(tin).balanceOf(address(this));
        MockToken(tout).transfer(to,q*2);
        (a,b)=z ? (int256(q),-int256(q*2)) : (-int256(q*2),int256(q));
        if(algebraStyle) ResearchMixedExecutor(payable(msg.sender)).algebraSwapCallback(a,b,"");
        else ResearchMixedExecutor(payable(msg.sender)).uniswapV3SwapCallback(a,b,"");
        require(Token(tin).balanceOf(address(this))-before==q,"underpaid");
    }
}
contract MockResearchV2 {
    address public token0;address public token1;
    address public factory=0x8909Dc15e40173Ff4699343b6eB8132c65e18eC6;
    constructor(address a,address b) {token0=a;token1=b;}
    function getReserves() external pure returns(uint112,uint112,uint32) {return (100 ether,100 ether,0);}
    function swap(uint256 out0,uint256 out1,address to,bytes calldata) external {
        if(out0>0) MockToken(token0).transfer(to,out0);
        if(out1>0) MockToken(token1).transfer(to,out1);
        uint256 b0=Token(token0).balanceOf(address(this));uint256 b1=Token(token1).balanceOf(address(this));
        uint256 in0=b0>100 ether-out0 ? b0-(100 ether-out0) : 0;
        uint256 in1=b1>100 ether-out1 ? b1-(100 ether-out1) : 0;
        require((b0*1000-in0*3)*(b1*1000-in1*3)>=uint256(100 ether)**2*1000**2,"K");
    }
}
contract MockResearchAeroV2 is MockResearchV2 {
    bool public stable=false;
    constructor(address a,address b) MockResearchV2(a,b) {factory=0x420DD381b31aEf6683db6B902084cB0FFECe40Da;}
    function getAmountOut(uint256 amount,address) external pure returns(uint256) {
        uint256 net=amount-amount*300/10000;
        return net*100 ether/(100 ether+net);
    }
}
contract ResearchMixedExecutorTest {
    Vm constant vm=Vm(address(uint160(uint256(keccak256("hevm cheat code")))));
    address constant WETH=0x4200000000000000000000000000000000000006;
    address constant F=0x33128a8fC17869897dcE68Ed026d694621f6FDfD;
    ResearchMixedExecutor executor;MockResearchV3 pool;MockToken target;
    function setUp() public {
        vm.etch(WETH,address(new MockToken()).code);
        vm.etch(F,address(new MockResearchFactory()).code);
        target=new MockToken();executor=new ResearchMixedExecutor();
        pool=new MockResearchV3(WETH,address(target));MockResearchFactory(F).seed(address(pool));
        target.seed(address(pool),100 ether);MockToken(WETH).seed(address(pool),100 ether);
        vm.deal(address(this),10 ether);
    }
    function steps(bool buy) internal view returns(ResearchStep[] memory s) {
        s=new ResearchStep[](1);s[0]=ResearchStep(3,address(pool),Key(WETH,address(target),3000,60,address(0)),buy);
    }
    function testV3BalancesAndTax() public {
        target.setTax(100);
        (uint256 paid,uint256 got,,,,uint256[] memory left)=executor.run{value:0.01 ether}(steps(true),uint128(0.01 ether),true);
        require(paid==0.01 ether && got==0.0198 ether && left[0]==0,"balance accounting");
    }
    function testPartialExitRecordsInventory() public {
        target.seed(address(executor),0.02 ether);pool.setFill(2500);
        (uint256 paid,uint256 got,,,,uint256[] memory left)=executor.run(steps(false),uint128(0.02 ether),false);
        require(paid==0.005 ether && got==0.01 ether && left[0]==0.015 ether,"partial accounting");
    }
    function testSelfReportedFactoryNotEnough() public {
        MockResearchFactory(F).seed(address(0));
        try executor.run{value:0.01 ether}(steps(true),uint128(0.01 ether),true) {revert("must reject");} catch {}
    }
    function testExternalCallbackRejected() public {
        try executor.uniswapV3SwapCallback(1,-1,"") {revert("must reject");} catch {}
    }
    function testV2TaxedInputUsesPoolReceivedAmount() public {
        address f=0x8909Dc15e40173Ff4699343b6eB8132c65e18eC6;
        vm.etch(f,address(new MockResearchFactory()).code);
        MockResearchV2 p=new MockResearchV2(WETH,address(target));MockResearchFactory(f).seed(address(p));
        MockToken(WETH).seed(address(p),100 ether);target.seed(address(p),100 ether);
        target.seed(address(executor),1 ether);target.setTax(1000);
        ResearchStep[] memory s=new ResearchStep[](1);
        s[0]=ResearchStep(2,address(p),Key(WETH,address(target),0,0,address(0)),false);
        (uint256 paid,uint256 got,,,,)=executor.run(s,uint128(1 ether),false);
        uint256 adjusted=uint256(0.9 ether)*997;
        require(paid==1 ether && got==adjusted*100 ether/(100 ether*1000+adjusted),"tax input");
    }
    function testV4AndV3MixedBalances() public {
        address pm=0x498581fF718922c3f8e6A244956aF099B2652b2b;
        vm.etch(pm,address(new MockManager()).code);
        MockToken finalToken=new MockToken();finalToken.seed(pm,100 ether);
        ResearchStep[] memory s=new ResearchStep[](2);s[0]=steps(true)[0];
        s[1]=ResearchStep(4,address(0),Key(address(target),address(finalToken),3000,60,address(0)),true);
        (uint256 paid,uint256 got,,,,uint256[] memory left)=executor.run{value:0.01 ether}(s,uint128(0.01 ether),true);
        require(paid==0.01 ether && got==0.04 ether && left[0]==0 && left[1]==0,"mixed route");
    }
    function testAeroV2UsesHistoricalPoolQuoteNotUniFee() public {
        address f=0x420DD381b31aEf6683db6B902084cB0FFECe40Da;
        vm.etch(f,address(new MockResearchFactory()).code);
        MockResearchAeroV2 p=new MockResearchAeroV2(WETH,address(target));MockResearchFactory(f).seed(address(p));
        MockToken(WETH).seed(address(p),100 ether);target.seed(address(p),100 ether);
        ResearchStep[] memory s=new ResearchStep[](1);
        s[0]=ResearchStep(5,address(p),Key(WETH,address(target),0,0,address(0)),true);
        (uint256 paid,uint256 got,,,,)=executor.run{value:1 ether}(s,uint128(1 ether),true);
        require(paid==1 ether && got==p.getAmountOut(1 ether,WETH),"Aero quote");
    }
    function testAlgebraRegisteredPoolAndCallback() public {
        address f=0xC5396866754799B9720125B104AE01d935Ab9C7b;
        vm.etch(f,address(new MockResearchFactory()).code);MockResearchFactory(f).seed(address(pool));pool.setAlgebra();
        ResearchStep[] memory s=steps(true);s[0].kind=6;
        (uint256 paid,uint256 got,,,,)=executor.run{value:0.01 ether}(s,uint128(0.01 ether),true);
        require(paid==0.01 ether && got==0.02 ether,"Algebra accounting");
    }
}
