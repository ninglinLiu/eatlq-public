// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;
import "../contracts/ResearchV4Executor.sol";

interface Vm {function etch(address,bytes calldata) external; function deal(address,uint256) external;}
contract MockToken {
    mapping(address=>uint256) public balanceOf;
    uint256 public taxBps;
    function seed(address who,uint256 amount) external {balanceOf[who]=amount;}
    function setTax(uint256 tax) external {taxBps=tax;}
    function deposit() external payable {balanceOf[msg.sender]+=msg.value;}
    function transfer(address to,uint256 amount) external returns(bool) {
        require(balanceOf[msg.sender]>=amount,"funds");
        balanceOf[msg.sender]-=amount;balanceOf[to]+=amount*(10000-taxBps)/10000;return true;
    }
}
interface Callback {function unlockCallback(bytes calldata) external returns(bytes memory);}
contract MockManager {
    uint256 debt; address currency; uint256 synced; bool locked;
    function unlock(bytes calldata data) external returns(bytes memory result) {
        require(!locked,"lock");locked=true;
        result=Callback(msg.sender).unlockCallback(data);
        require(debt==0,"unpaid");locked=false;
    }
    function swap(Key calldata,SwapParams calldata p,bytes calldata) external returns(int256) {
        require(locked && p.amountSpecified<0,"swap");
        uint128 qty=uint128(uint256(-p.amountSpecified));debt=qty;
        int128 a=p.zeroForOne ? -int128(qty) : int128(qty*2);
        int128 b=p.zeroForOne ? int128(qty*2) : -int128(qty);
        return (int256(a)<<128) | int256(uint256(uint128(b)));
    }
    function sync(address token) external {currency=token;synced=Token(token).balanceOf(address(this));}
    function settle() external payable returns(uint256 paid) {
        paid=Token(currency).balanceOf(address(this))-synced;
        require(paid==debt,"taxed-input-or-underpayment");debt=0;
    }
    function take(address token,address to,uint256 amount) external {MockToken(token).transfer(to,amount);}
}
contract ResearchV4ExecutorTest {
    Vm constant vm=Vm(address(uint160(uint256(keccak256("hevm cheat code")))));
    address constant PM=0x498581fF718922c3f8e6A244956aF099B2652b2b;
    address constant WETH=0x4200000000000000000000000000000000000006;
    ResearchV4Executor executor;
    MockToken target;
    function setUp() public {
        vm.etch(PM,address(new MockManager()).code);
        vm.etch(WETH,address(new MockToken()).code);
        target=new MockToken();executor=new ResearchV4Executor();
        target.seed(PM,100 ether);MockToken(WETH).seed(PM,100 ether);
        vm.deal(address(this),10 ether);
    }
    function route(bool buy) internal view returns(Key[] memory keys,bool[] memory dirs) {
        keys=new Key[](1);dirs=new bool[](1);
        (address a,address b)=WETH<address(target) ? (WETH,address(target)) : (address(target),WETH);
        keys[0]=Key(a,b,3000,60,address(0));dirs[0]=(buy ? WETH : address(target))==a;
    }
    function testBuyMeasuresRealReceivedNotPoolNominal() public {
        target.setTax(100);(Key[] memory keys,bool[] memory dirs)=route(true);
        (uint256 spent,uint256 got,uint256 gasUsed,,)=executor.run{value:0.01 ether}(keys,dirs,uint128(0.01 ether),true);
        require(spent==0.01 ether && got==0.0198 ether && gasUsed>0,"buy accounting");
        require(target.balanceOf(address(executor))==got,"wallet delta");
    }
    function testSellSpendsExactInjectedInventory() public {
        target.seed(address(executor),0.02 ether);(Key[] memory keys,bool[] memory dirs)=route(false);
        (uint256 spent,uint256 got,,,)=executor.run(keys,dirs,uint128(0.02 ether),false);
        require(spent==0.02 ether && got==0.04 ether,"sell accounting");
        require(target.balanceOf(address(executor))==0,"leftover");
    }
    function testTaxedInputMustNotPretendToSettle() public {
        target.seed(address(executor),0.02 ether);target.setTax(100);
        (Key[] memory keys,bool[] memory dirs)=route(false);
        try executor.run(keys,dirs,uint128(0.02 ether),false) {revert("must fail");} catch {}
    }
    function testUnauthorizedCallbackRejected() public {
        try executor.unlockCallback("") {revert("must fail");} catch {}
    }
}
