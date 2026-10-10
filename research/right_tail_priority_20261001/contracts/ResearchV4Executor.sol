// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

// Research only. This contract is never deployed or funded: its runtime is used
// as an eth_call state override to measure historical transfer settlement.
struct Key {address currency0; address currency1; uint24 fee; int24 tickSpacing; address hooks;}
struct SwapParams {bool zeroForOne; int256 amountSpecified; uint160 sqrtPriceLimitX96;}
interface Manager {
    function unlock(bytes calldata data) external returns (bytes memory);
    function swap(Key calldata key, SwapParams calldata params, bytes calldata hookData) external returns (int256);
    function sync(address currency) external;
    function settle() external payable returns (uint256);
    function take(address currency, address to, uint256 amount) external;
}
interface Token {function balanceOf(address) external view returns(uint256);}
interface Wrapped {function deposit() external payable;}

contract ResearchV4Executor {
    address constant PM = 0x498581fF718922c3f8e6A244956aF099B2652b2b;
    address constant WETH = 0x4200000000000000000000000000000000000006;
    address private caller;
    uint160 constant MIN_SQRT = 4295128739;
    uint160 constant MAX_SQRT = 1461446703485210103287273052203988822378723970342;
    receive() external payable {}
    // Some hooks consult the router's original caller.
    function msgSender() external view returns(address) {return caller;}
    function balance(address currency) public view returns(uint256) {
        return currency == address(0) ? address(this).balance : Token(currency).balanceOf(address(this));
    }
    function run(Key[] calldata keys, bool[] calldata directions, uint128 amount, bool wrapInput)
        external payable returns(uint256 spent, uint256 received, uint256 measuredGas, uint256 baseFee, uint256 timestamp)
    {
        uint256 startGas = gasleft();
        require(keys.length>0 && keys.length==directions.length && keys.length<=4, "route");
        require(caller==address(0), "reentrant");
        caller=msg.sender;
        address input=directions[0] ? keys[0].currency0 : keys[0].currency1;
        address output=directions[keys.length-1] ? keys[keys.length-1].currency1 : keys[keys.length-1].currency0;
        require(input!=output, "cycle");
        uint256 beforeInput=balance(input);
        if (wrapInput) {
            require(input==WETH && msg.value==amount, "wrap");
            Wrapped(WETH).deposit{value:amount}();
            beforeInput+=amount;
        }
        uint256 beforeOutput=balance(output);
        Manager(PM).unlock(abi.encode(keys,directions,amount));
        spent=beforeInput-balance(input);
        received=balance(output)-beforeOutput;
        caller=address(0);
        measuredGas=startGas-gasleft();
        return (spent,received,measuredGas,block.basefee,block.timestamp);
    }
    function unlockCallback(bytes calldata data) external returns(bytes memory) {
        require(msg.sender==PM && caller!=address(0), "callback");
        (Key[] memory keys,bool[] memory directions,uint128 amount)=abi.decode(data,(Key[],bool[],uint128));
        uint256 qty=amount;
        for (uint256 i=0;i<keys.length;i++) {
            require(qty>0 && qty<=type(uint128).max, "quantity");
            address input=directions[i] ? keys[i].currency0 : keys[i].currency1;
            address output=directions[i] ? keys[i].currency1 : keys[i].currency0;
            if (i>0) require(input==(directions[i-1] ? keys[i-1].currency1 : keys[i-1].currency0), "join");
            int256 delta=Manager(PM).swap(keys[i],SwapParams(directions[i],-int256(qty),
                directions[i] ? MIN_SQRT+1 : MAX_SQRT-1),"");
            int128 d0=int128(delta>>128);int128 d1=int128(delta);
            int128 din=directions[i] ? d0 : d1;int128 dout=directions[i] ? d1 : d0;
            require(din<=0 && dout>=0, "sign");
            uint256 debt=uint256(-int256(din));
            Manager(PM).sync(input);
            if (input==address(0)) Manager(PM).settle{value:debt}();
            else {
                (bool ok,bytes memory result)=input.call(abi.encodeWithSignature("transfer(address,uint256)",PM,debt));
                require(ok && (result.length==0 || abi.decode(result,(bool))), "payment");
                Manager(PM).settle();
            }
            uint256 before=balance(output);
            Manager(PM).take(output,address(this),uint256(uint128(dout)));
            qty=balance(output)-before;
        }
        return abi.encode(qty);
    }
}
