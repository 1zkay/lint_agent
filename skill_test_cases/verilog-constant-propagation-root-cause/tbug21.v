// 顶层模块 - RTL 门级描述
module top_module (
    input  wire clk,
    input  wire rst_n,      // 低电平有效的异步复位信号
    input  wire in1,        // 最底下的输入信号
    input  wire in2,
    output wire out1,
    output wire out2
);

    // 内部信号声明
    wire flop_data_constant;    // FlopDataConstant - 触发器数据输入
    wire flop_out;              // 触发器输出
    wire rtlc_l4;               // 缓冲器 rtlc_l4 输出
    wire rtlc_l5;               // 与门 rtlc_l5 输出
    wire rtlc_l7;               // 反相器 rtlc_l7 输出

    // 输出赋值
    assign out1 = flop_out;

    //============================================================
    // 触发器实例: flopInst (FLOP)
    //============================================================
    flop flopInst (
        .clk    (clk),
        .rst_n  (rst_n),        // 增加复位信号
        .in     (rtlc_l7),
        .out    (flop_out)
    );

    //============================================================
    // 组合逻辑部分 - 门级描述
    //============================================================
    
    // FlopDataConstant - 常量源 (图中显示为独立信号)
    assign flop_data_constant = 1'b0;  // 假设为常数0，可根据实际调整

    // 缓冲器 rtlc_l4: 输入 flop_data_constant，输出 rtlc_l4
    buf rtlc_l4_inst (rtlc_l4, flop_data_constant);  // rtlc_l4 = 0

    // 与门 rtlc_l5 (DisabledAnd): 输入为 rtlc_l4 和 in1
    and rtlc_l5_inst (rtlc_l5, rtlc_l4, in1);

    // 反相器 rtlc_l7: 输入 rtlc_l5，输出 rtlc_l7
    not rtlc_l7_inst (rtlc_l7, rtlc_l5);

    // 或门 rtlc_l9 (DisabledOr): 输入为 in2 和 rtlc_l7，输出 out2
    or rtlc_l9_inst (out2, in2, rtlc_l7);

endmodule

//============================================================
// 触发器模块定义 - 行为级描述 (带异步复位)
//============================================================
module flop (
    input  wire clk,
    input  wire rst_n,      // 低电平有效的异步复位信号
    input  wire in,
    output reg  out
);

    // 异步复位：当 rst_n 为低电平时，立即复位
    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            out <= 1'b0;    // 复位时输出清零
        end else begin
            out <= in;      // 正常工作时，输入传递到输出
        end
    end

endmodule