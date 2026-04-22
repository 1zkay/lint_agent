module case_missing_2bit (
    input  wire [1:0] in3,
    input  wire       in1,
    input  wire       in2,
    input  wire [3:0] mem,
    output reg  [2:0] out
);

always @(*) begin
    case (in3)
        2'b00: out[0] = in1 || mem[0];
        2'b01: out[1] = in1 && mem[1];
        2'b11: out[2] = in2 && mem[3];
    endcase
end

endmodule
