module dead_loop_static_range (
    input  wire       clk,
    input  wire [7:0] data0,
    input  wire [7:0] data1,
    input  wire [7:0] data2,
    output reg  [7:0] acc
);
integer idx;

always @(posedge clk) begin
    acc <= 8'h00;
    for (idx = 0; idx < 4; idx = idx + 1) begin
        if (idx == 4) begin
            acc <= data0;       // unreachable: idx only takes 0, 1, 2, 3
        end else if (idx >= 5) begin
            acc <= data1;       // unreachable: idx only takes 0, 1, 2, 3
        end else begin
            acc <= acc ^ data2;
        end
    end
end

endmodule
